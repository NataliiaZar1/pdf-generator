"""Простая программа: выбирает счёт и сохраняет его в PDF."""

import csv
import json
import os
import platform
import subprocess
from pathlib import Path

# Папки ищем рядом с этим файлом, а не от того места, откуда запустили команду.
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
TEMPLATES_DIR = BASE_DIR / "templates"
OUTPUT_DIR = BASE_DIR / "output"
LINE = "=" * 40

# Общие сведения о счёте. В CSV они повторяются на каждой строке товара.
INVOICE_FIELDS = (
    "invoice_id",
    "invoice_date",
    "customer_name",
    "customer_address",
    "total",
)
# В CSV у каждой строки есть свой товар. В JSON товары лежат списком items.
CSV_ITEM_FIELDS = ("name", "quantity", "price", "subtotal")

# Библиотеки ниже ставятся отдельно. Если их ещё нет, программа объяснит это по-русски.
try:
    from jinja2 import Environment, FileSystemLoader, select_autoescape
except ImportError:
    Environment = None

try:
    from weasyprint import HTML
except Exception as error:
    HTML = None
    WEASYPRINT_IMPORT_ERROR = error
else:
    WEASYPRINT_IMPORT_ERROR = None


def find_data_files():
    """Возвращает все CSV и JSON из папки data."""
    files = []
    for pattern in ("*.csv", "*.json"):
        files.extend(DATA_DIR.glob(pattern))
    return sorted(files, key=lambda path: path.name.lower())


def find_templates():
    """Возвращает все HTML-шаблоны из папки templates."""
    return sorted(TEMPLATES_DIR.glob("*.html"), key=lambda path: path.name.lower())


def show_menu(title, options):
    """Печатает заголовок и нумерованный список."""
    print()
    print(title)
    print()
    for number, option in enumerate(options, start=1):
        print(f"{number}. {option}")
    print()


def ask_choice(prompt, options):
    """Просит ввести номер пункта и повторяет вопрос при ошибке."""
    while True:
        answer = input(prompt).strip()
        if answer.isdigit():
            number = int(answer)
            if 1 <= number <= len(options):
                return options[number - 1]
        print("Неверный выбор. Введите номер из списка и попробуйте ещё раз.")


def choose_file(files):
    """Показывает файлы данных и возвращает выбранный."""
    show_menu("Доступные файлы данных:", [path.name for path in files])
    return ask_choice("Выберите файл данных: ", files)


def choose_template(templates):
    """Показывает шаблоны и возвращает выбранный."""
    show_menu("Доступные шаблоны:", [path.name for path in templates])
    return ask_choice("Выберите шаблон: ", templates)


def choose_invoice(invoices):
    """Показывает номера счетов. Можно ввести номер пункта или сам invoice_id."""
    show_menu("Доступные счета:", [invoice["invoice_id"] for invoice in invoices])
    invoices_by_id = {invoice["invoice_id"]: invoice for invoice in invoices}

    while True:
        answer = input("Выберите invoice ID: ").strip()
        if answer.isdigit():
            number = int(answer)
            if 1 <= number <= len(invoices):
                return invoices[number - 1]
            print("Неверный номер. Введите номер из списка и попробуйте ещё раз.")
            continue

        invoice = invoices_by_id.get(answer)
        if invoice is not None:
            return invoice

        print(f"Счёт «{answer}» не найден. Введите номер из списка и попробуйте ещё раз.")


def to_number(value, field_name, source, place):
    """Превращает текст в число. Понимает и запятую, и точку."""
    text = str(value).strip().replace(" ", "").replace(",", ".")
    try:
        return float(text)
    except ValueError as error:
        raise ValueError(
            f"В файле {source}, {place}: поле «{field_name}» должно быть числом."
        ) from error


def normalize_quantity(value):
    """Целое количество показывает без лишнего нуля после запятой."""
    if float(value).is_integer():
        return int(value)
    return float(value)


def read_invoice_item(raw_item, source, place):
    """Проверяет одну позицию счёта и приводит числа к нужному виду."""
    if not isinstance(raw_item, dict):
        raise ValueError(f"В файле {source}, {place}: позиция товара записана неправильно.")

    name = str(raw_item.get("name") or "").strip()
    if not name:
        raise ValueError(f"В файле {source}, {place}: у товара нет названия.")

    return {
        "name": name,
        "quantity": normalize_quantity(to_number(raw_item.get("quantity"), "quantity", source, place)),
        "price": to_number(raw_item.get("price"), "price", source, place),
        "subtotal": to_number(raw_item.get("subtotal"), "subtotal", source, place),
    }


def build_invoice(raw_invoice, items, source, place):
    """Собирает проверенный словарь одного счёта."""
    if not isinstance(raw_invoice, dict):
        raise ValueError(f"В файле {source}, {place}: счёт записан неправильно.")

    invoice_id = str(raw_invoice.get("invoice_id") or "").strip()
    if not invoice_id:
        raise ValueError(f"В файле {source}, {place}: отсутствует invoice_id.")

    invoice_date = str(raw_invoice.get("invoice_date") or "").strip()
    customer_name = str(raw_invoice.get("customer_name") or "").strip()
    customer_address = str(raw_invoice.get("customer_address") or "").strip()
    if not invoice_date or not customer_name or not customer_address:
        raise ValueError(
            f"В файле {source}, {place}: у счёта {invoice_id} не заполнены дата, клиент или адрес."
        )
    if not items:
        raise ValueError(f"В файле {source}, {place}: у счёта {invoice_id} нет товаров.")

    return {
        "invoice_id": invoice_id,
        "invoice_date": invoice_date,
        "customer_name": customer_name,
        "customer_address": customer_address,
        "items": items,
        "total": to_number(raw_invoice.get("total"), "total", source, place),
    }


def load_csv(path):
    """Читает CSV. Каждая строка — один товар, строки с одним invoice_id собираются в счёт."""
    try:
        with path.open(encoding="utf-8-sig", newline="") as file:
            reader = csv.DictReader(file)
            rows = list(reader)
            fieldnames = reader.fieldnames or []
    except OSError as error:
        raise ValueError(f"Не удалось прочитать файл {path.name}: {error}") from error

    required = list(INVOICE_FIELDS) + list(CSV_ITEM_FIELDS)
    missing = [name for name in required if name not in fieldnames]
    if missing:
        missing_text = ", ".join(missing)
        raise ValueError(f"В файле {path.name} не хватает столбцов: {missing_text}.")
    if not rows:
        raise ValueError(f"Файл {path.name} пустой: в нём нет счетов.")

    grouped = {}
    order = []
    for line_number, row in enumerate(rows, start=2):
        place = f"строка {line_number}"
        invoice_id = str(row.get("invoice_id") or "").strip()
        if not invoice_id:
            raise ValueError(f"В файле {path.name}, {place}: отсутствует invoice_id.")

        item = read_invoice_item(row, path.name, place)
        if invoice_id not in grouped:
            grouped[invoice_id] = build_invoice(row, [item], path.name, place)
            order.append(invoice_id)
            continue

        # У одного счёта общие поля должны совпадать во всех строках.
        invoice = grouped[invoice_id]
        same_invoice = (
            invoice["invoice_date"] == str(row.get("invoice_date") or "").strip()
            and invoice["customer_name"] == str(row.get("customer_name") or "").strip()
            and invoice["customer_address"] == str(row.get("customer_address") or "").strip()
            and invoice["total"] == to_number(row.get("total"), "total", path.name, place)
        )
        if not same_invoice:
            raise ValueError(
                f"В файле {path.name} у счёта {invoice_id} различаются дата, клиент, адрес или итог."
            )
        invoice["items"].append(item)

    return [grouped[invoice_id] for invoice_id in order]


def load_json(path):
    """Читает JSON и проверяет, что внутри список счетов нужного вида."""
    try:
        with path.open(encoding="utf-8-sig") as file:
            content = json.load(file)
    except json.JSONDecodeError as error:
        raise ValueError(
            f"Файл {path.name} повреждён или имеет неправильный формат JSON "
            f"(ошибка в строке {error.lineno})."
        ) from error
    except OSError as error:
        raise ValueError(f"Не удалось прочитать файл {path.name}: {error}") from error

    if isinstance(content, dict) and isinstance(content.get("invoices"), list):
        content = content["invoices"]
    elif isinstance(content, dict):
        content = [content]
    if not isinstance(content, list):
        raise ValueError(f"Файл {path.name} должен содержать список счетов.")
    if not content:
        raise ValueError(f"Файл {path.name} пустой: в нём нет счетов.")

    invoices = []
    seen_ids = set()
    for index, raw_invoice in enumerate(content, start=1):
        place = f"счёт номер {index}"
        if not isinstance(raw_invoice, dict):
            raise ValueError(f"В файле {path.name}, {place}: счёт записан неправильно.")

        invoice_id = str(raw_invoice.get("invoice_id") or "").strip()
        if not invoice_id:
            raise ValueError(f"В файле {path.name}, {place}: отсутствует invoice_id.")

        raw_items = raw_invoice.get("items")
        if not isinstance(raw_items, list):
            raise ValueError(f"В файле {path.name} у счёта {invoice_id} поле items должно быть списком.")

        items = [
            read_invoice_item(raw_item, path.name, f"{place}, товар {item_index}")
            for item_index, raw_item in enumerate(raw_items, start=1)
        ]
        invoice = build_invoice(raw_invoice, items, path.name, place)
        if invoice["invoice_id"] in seen_ids:
            raise ValueError(f"В файле {path.name} счёт {invoice['invoice_id']} указан дважды.")
        seen_ids.add(invoice["invoice_id"])
        invoices.append(invoice)

    return invoices


def load_data(path):
    """Выбирает способ чтения по расширению файла."""
    extension = path.suffix.lower()
    if extension == ".csv":
        return load_csv(path)
    if extension == ".json":
        return load_json(path)
    raise ValueError(f"Файл {path.name} не является CSV или JSON.")


def format_money(value):
    """Делает из числа строку вида 28 000,00."""
    return f"{float(value):,.2f}".replace(",", " ").replace(".", ",")


def prepare_invoice(invoice):
    """Добавляет готовые текстовые суммы, чтобы шаблон оставался простым."""
    prepared = dict(invoice)
    prepared["items"] = []
    for item in invoice["items"]:
        prepared["items"].append({
            "name": item["name"],
            "quantity_text": str(item["quantity"]),
            "price_text": format_money(item["price"]),
            "subtotal_text": format_money(item["subtotal"]),
        })
    prepared["total_text"] = format_money(invoice["total"])
    return prepared


def ensure_output_dir():
    """Создаёт папку output, если её нет. Это безопасное место для готовых PDF."""
    if OUTPUT_DIR.exists() and not OUTPUT_DIR.is_dir():
        raise ValueError(
            "Имя output уже занято файлом. Переименуйте этот файл и запустите программу снова."
        )
    if not OUTPUT_DIR.exists():
        OUTPUT_DIR.mkdir(parents=True)
        print("Папки output не было. Я создал её: сюда будут сохраняться PDF.")


def build_output_path(invoice_id):
    """Готовит имя PDF. Если такой файл уже есть, добавляет номер, а не затирает его."""
    ensure_output_dir()
    safe_name = "".join(char if char.isalnum() or char in "-_" else "_" for char in invoice_id)
    if not safe_name:
        safe_name = "invoice"

    path = OUTPUT_DIR / f"{safe_name}.pdf"
    if not path.exists():
        return path

    number = 2
    while True:
        candidate = OUTPUT_DIR / f"{safe_name}_{number}.pdf"
        if not candidate.exists():
            print(
                f"Файл {path.name} уже существует. "
                f"Чтобы не затереть его, сохраняю новый PDF как {candidate.name}."
            )
            return candidate
        number += 1


def library_help():
    """Текст, который видит пользователь, если библиотеки ещё не готовы."""
    lines = [
        "Пока нельзя создать PDF: не установлены нужные части программы.",
        "Сначала установите MSYS2 и библиотеку Pango, затем зависимости Python.",
        "Пошаговая инструкция есть в файле README.md, раздел «Установка WeasyPrint на Windows».",
    ]
    if Environment is None:
        lines.append("Сейчас не найдена библиотека Jinja2.")
    if HTML is None:
        lines.append(f"Сейчас не удалось подключить WeasyPrint: {WEASYPRINT_IMPORT_ERROR}")
    return "\n".join(lines)


def generate_pdf(template_path, invoice):
    """Подставляет данные в HTML-шаблон и сохраняет PDF."""
    if Environment is None or HTML is None:
        raise RuntimeError(library_help())

    environment = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
    )
    template = environment.get_template(template_path.name)
    html = template.render(invoice=prepare_invoice(invoice))
    output_path = build_output_path(invoice["invoice_id"])

    # base_url нужен, чтобы шаблон нашёл файл шрифта в папке fonts.
    HTML(string=html, base_url=str(TEMPLATES_DIR)).write_pdf(str(output_path))
    return output_path


def open_pdf(path):
    """Открывает PDF программой, которая выбрана в системе для таких файлов."""
    system_name = platform.system()
    try:
        if system_name == "Windows":
            os.startfile(path)  # type: ignore[attr-defined]
        elif system_name == "Darwin":
            subprocess.run(["open", str(path)], check=True)
        else:
            subprocess.run(["xdg-open", str(path)], check=True)
    except Exception:
        print("Не удалось автоматически открыть PDF.")
        print("Файл сохранён здесь:")
        print(path.resolve())
        return

    print("PDF создан и открыт.")
    print(path.resolve())


def main():
    """Ведёт пользователя по шагам: файл, шаблон, счёт, PDF."""
    print(LINE)
    print("           PDF GENERATOR")
    print(LINE)

    if not DATA_DIR.is_dir():
        print("Папка data не найдена. Положите в неё CSV или JSON со счетами.")
        return
    if not TEMPLATES_DIR.is_dir():
        print("Папка templates не найдена. Положите в неё HTML-шаблоны счетов.")
        return

    data_files = find_data_files()
    if not data_files:
        print("В папке data нет файлов CSV или JSON.")
        return

    templates = find_templates()
    if not templates:
        print("В папке templates нет HTML-шаблонов.")
        return

    data_file = choose_file(data_files)
    template_file = choose_template(templates)

    try:
        invoices = load_data(data_file)
    except ValueError as error:
        print(error)
        return

    invoice = choose_invoice(invoices)

    try:
        pdf_path = generate_pdf(template_file, invoice)
    except Exception as error:
        print("Не удалось создать PDF.")
        print(error)
        return

    print()
    print(f"Счёт {invoice['invoice_id']} сохранён: {pdf_path.name}")
    open_pdf(pdf_path)


if __name__ == "__main__":
    main()
