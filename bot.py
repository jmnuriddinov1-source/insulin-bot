import base64
import difflib
import json
import logging
import math
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

try:
    from openai import OpenAI
except Exception:
    OpenAI = None

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

DATA_DIR = Path(os.getenv("DATA_DIR", "."))
DB_PATH = DATA_DIR / "bot_data.sqlite3"
PRODUCTS_JSON = DATA_DIR / "products_ru.json"

(
    CHOOSING,
    WAITING_TDD,
    WAITING_GLUCOSE,
    WAITING_GRAMS,
    WAITING_PHOTO,
    WAITING_ADD_CUSTOM_NAME,
    WAITING_ADD_CUSTOM_C,
    WAITING_ADD_CUSTOM_P,
    WAITING_ADD_CUSTOM_F,
) = range(9)

MAIN_MENU = ReplyKeyboardMarkup(
    [
        [KeyboardButton("🍳 Завтрак"), KeyboardButton("🍲 Обед")],
        [KeyboardButton("🌙 Ужин"), KeyboardButton("🍎 Перекус")],
        [KeyboardButton("📜 История"), KeyboardButton("⚙️ Настройки")],
        [KeyboardButton("❓ Помощь"), KeyboardButton("♻️ Сброс")],
    ],
    resize_keyboard=True,
)

MEAL_MENU = ReplyKeyboardMarkup(
    [
        [KeyboardButton("🍞 Быстрые продукты"), KeyboardButton("📚 Категории")],
        [KeyboardButton("📷 Фото еды"), KeyboardButton("🔎 Найти продукт")],
        [KeyboardButton("✅ Рассчитать"), KeyboardButton("➕ Свой продукт")],
        [KeyboardButton("↩️ Назад")],
    ],
    resize_keyboard=True,
)

SETTINGS_MENU = ReplyKeyboardMarkup(
    [
        [KeyboardButton("💉 Шаг шприца 1"), KeyboardButton("💉 Шаг шприца 0.5")],
        [KeyboardButton("💉 Шаг шприца 0.1"), KeyboardButton("🍞 1 ХЕ = 12 г")],
        [KeyboardButton("🍞 1 ХЕ = 10 г"), KeyboardButton("↩️ Назад")],
    ],
    resize_keyboard=True,
)

CATEGORY_BUTTONS = {
    "bread": "🍞 Хлеб и крупы",
    "vegetables": "🥔 Овощи и гарниры",
    "fruits": "🍎 Фрукты и соки",
    "protein": "🍗 Мясо, яйца, рыба",
    "ready": "🥟 Готовые блюда",
    "drinks": "☕ Напитки",
    "sweets": "🍪 Сладкое",
    "fastfood": "🍔 Фастфуд",
}
CATEGORY_NAMES = {v: k for k, v in CATEGORY_BUTTONS.items()}
MEAL_NAMES = {
    "🍳 Завтрак": "Завтрак",
    "🍲 Обед": "Обед",
    "🌙 Ужин": "Ужин",
    "🍎 Перекус": "Перекус",
}
QUICK_PRODUCTS = [
    "хлеб белый", "хлеб черный", "рис вареный", "плов", "картошка пюре", "яблоко",
    "банан", "курица отварная", "сосиски", "яйцо куриное", "суп куриный", "сок яблочный",
]


@dataclass
class Portion:
    product_id: int
    name: str
    grams: float
    carbs100: float
    proteins100: float
    fats100: float
    source: str = "manual"
    note: str = ""

    @property
    def carbs(self) -> float:
        return self.carbs100 * self.grams / 100.0

    @property
    def proteins(self) -> float:
        return self.proteins100 * self.grams / 100.0

    @property
    def fats(self) -> float:
        return self.fats100 * self.grams / 100.0


def fmt(value: float, digits: int = 1) -> str:
    txt = f"{value:.{digits}f}"
    txt = txt.rstrip("0").rstrip(".")
    return txt if txt else "0"


# ---------- DB ----------

def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                injection_step REAL NOT NULL DEFAULT 1.0,
                xe_grams REAL NOT NULL DEFAULT 12.0,
                rounding_mode TEXT NOT NULL DEFAULT 'nearest'
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                aliases TEXT NOT NULL DEFAULT '[]',
                category TEXT NOT NULL,
                carbs REAL NOT NULL,
                proteins REAL NOT NULL,
                fats REAL NOT NULL,
                source TEXT NOT NULL DEFAULT 'seed'
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS meal_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                meal_type TEXT NOT NULL,
                total_xe REAL NOT NULL,
                total_carbs REAL NOT NULL,
                total_proteins REAL NOT NULL,
                total_fats REAL NOT NULL,
                tdd REAL,
                glucose REAL,
                dose_exact REAL,
                dose_rounded REAL,
                details_json TEXT NOT NULL
            )
            """
        )
        conn.commit()
    seed_products()


def seed_products() -> None:
    data = json.loads(PRODUCTS_JSON.read_text(encoding="utf-8"))
    with get_conn() as conn:
        count = conn.execute("SELECT COUNT(*) c FROM products").fetchone()["c"]
        if count:
            return
        conn.executemany(
            "INSERT INTO products(name, aliases, category, carbs, proteins, fats, source) VALUES (?, ?, ?, ?, ?, ?, 'seed')",
            [
                (
                    item["name"],
                    json.dumps(item.get("aliases", []), ensure_ascii=False),
                    item["category"],
                    item["carbs"],
                    item["proteins"],
                    item["fats"],
                )
                for item in data
            ],
        )
        conn.commit()


def get_settings(user_id: int) -> Dict[str, Any]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM user_settings WHERE user_id=?", (user_id,)).fetchone()
        if not row:
            conn.execute("INSERT INTO user_settings(user_id) VALUES (?)", (user_id,))
            conn.commit()
            row = conn.execute("SELECT * FROM user_settings WHERE user_id=?", (user_id,)).fetchone()
        return dict(row)


def update_settings(user_id: int, **kwargs: Any) -> None:
    if not kwargs:
        return
    cols = ", ".join(f"{k}=?" for k in kwargs)
    values = list(kwargs.values()) + [user_id]
    with get_conn() as conn:
        conn.execute(f"UPDATE user_settings SET {cols} WHERE user_id=?", values)
        conn.commit()


def row_to_product(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "aliases": json.loads(row["aliases"] or "[]"),
        "category": row["category"],
        "carbs": row["carbs"],
        "proteins": row["proteins"],
        "fats": row["fats"],
        "source": row["source"],
    }


def search_products(query: str, limit: int = 8) -> List[Dict[str, Any]]:
    query = query.strip().lower()
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM products").fetchall()
    products = [row_to_product(r) for r in rows]
    scored: List[Tuple[float, Dict[str, Any]]] = []
    for p in products:
        names = [p["name"].lower(), *[a.lower() for a in p.get("aliases", [])]]
        best = 0.0
        if any(query == n for n in names):
            best = 1.0
        elif any(query in n for n in names):
            best = 0.92
        else:
            best = max(difflib.SequenceMatcher(None, query, n).ratio() for n in names)
        if best >= 0.45:
            scored.append((best, p))
    scored.sort(key=lambda x: (-x[0], x[1]["name"]))
    return [p for _, p in scored[:limit]]


def list_by_category(category: str) -> List[Dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM products WHERE category=? ORDER BY name", (category,)).fetchall()
    return [row_to_product(r) for r in rows]


def get_product_by_id(product_id: int) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    return row_to_product(row) if row else None


def add_custom_product(name: str, carbs: float, proteins: float, fats: float) -> int:
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO products(name, aliases, category, carbs, proteins, fats, source) VALUES (?, '[]', 'ready', ?, ?, ?, 'custom')",
            (name, carbs, proteins, fats),
        )
        conn.commit()
        row = conn.execute("SELECT id FROM products WHERE name=?", (name,)).fetchone()
    return int(row["id"])


def save_history(user_id: int, meal_type: str, items: List[Portion], total_xe: float, total_carbs: float, total_proteins: float, total_fats: float, tdd: float, glucose: float, dose_exact: float, dose_rounded: float) -> None:
    details = [vars(i) for i in items]
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO meal_history(user_id, meal_type, total_xe, total_carbs, total_proteins, total_fats, tdd, glucose, dose_exact, dose_rounded, details_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, meal_type, total_xe, total_carbs, total_proteins, total_fats, tdd, glucose, dose_exact, dose_rounded, json.dumps(details, ensure_ascii=False)),
        )
        conn.commit()


def get_history(user_id: int, limit: int = 5) -> List[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM meal_history WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()


# ---------- Calculations ----------

def xe_for_carbs(carbs: float, xe_grams: float) -> float:
    return carbs / xe_grams if xe_grams else 0.0


def icr_from_tdd(tdd: float) -> float:
    return 500.0 / tdd


def insulin_per_xe_from_tdd(tdd: float, xe_grams: float) -> float:
    return xe_grams / icr_from_tdd(tdd)


def round_step(value: float, step: float, mode: str) -> float:
    if step <= 0:
        return value
    if mode == "down":
        return math.floor(value / step) * step
    if mode == "up":
        return math.ceil(value / step) * step
    return round(value / step) * step


def meal_totals(items: List[Portion], xe_grams: float) -> Dict[str, float]:
    total_carbs = sum(i.carbs for i in items)
    total_proteins = sum(i.proteins for i in items)
    total_fats = sum(i.fats for i in items)
    return {
        "carbs": total_carbs,
        "proteins": total_proteins,
        "fats": total_fats,
        "xe": xe_for_carbs(total_carbs, xe_grams),
    }


# ---------- Session helpers ----------

def reset_session(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["meal"] = {
        "meal_type": None,
        "items": [],
        "selected_product": None,
        "tdd": None,
        "glucose": None,
    }


def get_session(context: ContextTypes.DEFAULT_TYPE) -> Dict[str, Any]:
    if "meal" not in context.user_data:
        reset_session(context)
    return context.user_data["meal"]


def add_item(context: ContextTypes.DEFAULT_TYPE, product: Dict[str, Any], grams: float, source: str = "manual", note: str = "") -> Portion:
    portion = Portion(
        product_id=product["id"],
        name=product["name"],
        grams=grams,
        carbs100=float(product["carbs"]),
        proteins100=float(product["proteins"]),
        fats100=float(product["fats"]),
        source=source,
        note=note,
    )
    get_session(context)["items"].append(portion)
    return portion


def portion_text(portion: Portion, xe_grams: float) -> str:
    return (
        f"✅ {portion.name} — {fmt(portion.grams)} г\n"
        f"На 100 г: У {fmt(portion.carbs100)} | Б {fmt(portion.proteins100)} | Ж {fmt(portion.fats100)}\n"
        f"В порции: У {fmt(portion.carbs)} | Б {fmt(portion.proteins)} | Ж {fmt(portion.fats)}\n"
        f"ХЕ: {fmt(xe_for_carbs(portion.carbs, xe_grams), 2)}"
    )


def meal_summary(context: ContextTypes.DEFAULT_TYPE, user_id: int, include_dose_preview: bool = False) -> str:
    settings = get_settings(user_id)
    xe_grams = settings["xe_grams"]
    session = get_session(context)
    items: List[Portion] = session["items"]
    if not items:
        return "Пока ничего не добавлено."
    lines = [f"🍽 {session['meal_type'] or 'Приём пищи'}"]
    for i, item in enumerate(items, start=1):
        lines.append(
            f"{i}. {item.name} {fmt(item.grams)} г — У {fmt(item.carbs)} / Б {fmt(item.proteins)} / Ж {fmt(item.fats)} / ХЕ {fmt(xe_for_carbs(item.carbs, xe_grams), 2)}"
        )
    totals = meal_totals(items, xe_grams)
    lines.append("")
    lines.append(f"Итого: У {fmt(totals['carbs'])} | Б {fmt(totals['proteins'])} | Ж {fmt(totals['fats'])}")
    lines.append(f"Итого ХЕ: {fmt(totals['xe'], 2)}")
    if include_dose_preview and session.get("tdd"):
        ins_xe = insulin_per_xe_from_tdd(float(session["tdd"]), xe_grams)
        exact = totals["xe"] * ins_xe
        rounded = round_step(exact, settings["injection_step"], settings["rounding_mode"])
        lines.append(f"Суточный инсулин: {fmt(float(session['tdd']), 2)}")
        if session.get("glucose") is not None:
            lines.append(f"Сахар перед едой: {fmt(float(session['glucose']), 1)}")
        lines.append(f"Инсулин на 1 ХЕ: {fmt(ins_xe, 2)} ед")
        lines.append(f"Точная доза: {fmt(exact, 2)} ед")
        lines.append(f"Округлённо под шаг {fmt(settings['injection_step'], 2)}: {fmt(rounded, 2)} ед")
    return "\n".join(lines)


# ---------- OpenAI photo ----------

def get_openai_client() -> OpenAI:
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY не задан")
    if OpenAI is None:
        raise RuntimeError("Пакет openai не установлен")
    return OpenAI(api_key=key)


def analyze_photo_with_openai(image_bytes: bytes, catalog_names: List[str]) -> Dict[str, Any]:
    client = get_openai_client()
    model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    catalog_sample = ", ".join(catalog_names[:180])
    prompt = (
        "Ты оцениваешь еду на фото для расчета БЖУ и ХЕ. "
        "Нужно определить 1-8 продуктов на тарелке или рядом, примерно оценить вес каждого в граммах и вернуть только JSON. "
        "Используй максимально близкие названия из списка каталога. "
        f"Каталог: {catalog_sample}. "
        "Формат ответа строго такой: "
        '{"items":[{"name":"яблоко","grams":100,"reason":"кусочки яблока"}],"note":"краткая пометка"}. '
        "Если не уверен, все равно дай лучшую оценку. Граммы только числа. Без markdown."
    )
    response = client.responses.create(
        model=model,
        input=[
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": prompt},
                    {"type": "input_image", "image_url": f"data:image/jpeg;base64,{b64}"},
                ],
            }
        ],
    )
    text = getattr(response, "output_text", "") or ""
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise ValueError(f"Не удалось разобрать ответ модели: {text[:300]}")
    data = json.loads(match.group(0))
    if not isinstance(data, dict) or "items" not in data:
        raise ValueError("Ответ модели без items")
    return data


# ---------- Keyboards ----------

def categories_inline() -> InlineKeyboardMarkup:
    buttons = []
    row: List[InlineKeyboardButton] = []
    for title in CATEGORY_NAMES.keys():
        row.append(InlineKeyboardButton(title, callback_data=f"cat:{CATEGORY_NAMES[title]}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(buttons)


def product_buttons(products: List[Dict[str, Any]], prefix: str = "pick") -> InlineKeyboardMarkup:
    buttons: List[List[InlineKeyboardButton]] = []
    row: List[InlineKeyboardButton] = []
    for p in products[:24]:
        row.append(InlineKeyboardButton(p["name"], callback_data=f"{prefix}:{p['id']}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("↩️ Назад к категориям", callback_data="back:categories")])
    return InlineKeyboardMarkup(buttons)


def quick_products_keyboard() -> InlineKeyboardMarkup:
    products = []
    for name in QUICK_PRODUCTS:
        found = search_products(name, limit=1)
        if found:
            products.append(found[0])
    return product_buttons(products)


# ---------- Handlers ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    reset_session(context)
    text = (
        "Привет. Этот бот считает Б/Ж/У, ХЕ и дозу инсулина на еду.\n\n"
        "Как пользоваться:\n"
        "1) Выбери Завтрак / Обед / Ужин / Перекус\n"
        "2) Добавляй продукты из списка или фото\n"
        "3) Нажми Рассчитать\n"
        "4) Введи сахар и суточный инсулин\n\n"
        "⚠️ Фото еды — только приблизительная оценка. Итог обязательно проверь вручную."
    )
    await update.message.reply_text(text, reply_markup=MAIN_MENU)
    return CHOOSING


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Нажми приём пищи, потом добавляй продукты.\n"
        "Можно выбрать из списка, написать название вручную или прислать фото еды.\n"
        "После кнопки ✅ Рассчитать бот спросит сахар и суточный инсулин и покажет дозу.",
        reply_markup=MAIN_MENU if not get_session(context).get("meal_type") else MEAL_MENU,
    )
    return CHOOSING


async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    reset_session(context)
    await update.message.reply_text("Сбросил текущий приём пищи.", reply_markup=MAIN_MENU)
    return CHOOSING


async def show_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    rows = get_history(update.effective_user.id)
    if not rows:
        await update.message.reply_text("История пока пустая.", reply_markup=MAIN_MENU)
        return CHOOSING
    parts = ["📜 Последние расчёты:"]
    for row in rows:
        parts.append(
            f"• {row['created_at'][:16]} | {row['meal_type']} | ХЕ {fmt(row['total_xe'],2)} | доза {fmt(row['dose_rounded'],2)} ед"
        )
    await update.message.reply_text("\n".join(parts), reply_markup=MAIN_MENU)
    return CHOOSING


async def settings_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    s = get_settings(update.effective_user.id)
    await update.message.reply_text(
        f"Текущие настройки:\nШаг шприца: {fmt(s['injection_step'],2)} ед\n1 ХЕ = {fmt(s['xe_grams'],2)} г углеводов",
        reply_markup=SETTINGS_MENU,
    )
    return CHOOSING


async def choose_meal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    meal_type = MEAL_NAMES[update.message.text]
    reset_session(context)
    session = get_session(context)
    session["meal_type"] = meal_type
    await update.message.reply_text(
        f"Выбран приём пищи: {meal_type}.\nДобавляй продукты кнопками, названием или фото.",
        reply_markup=MEAL_MENU,
    )
    await update.message.reply_text("Выбери категорию или быстрые продукты.", reply_markup=categories_inline())
    return CHOOSING


async def choose_quick_products(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Быстрые продукты:", reply_markup=quick_products_keyboard())
    return CHOOSING


async def choose_categories(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Категории:", reply_markup=categories_inline())
    return CHOOSING


async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data
    if data.startswith("cat:"):
        category = data.split(":", 1)[1]
        products = list_by_category(category)
        await query.edit_message_text(f"Категория: {CATEGORY_BUTTONS[category]}", reply_markup=product_buttons(products))
        return CHOOSING
    if data.startswith("pick:"):
        product_id = int(data.split(":", 1)[1])
        product = get_product_by_id(product_id)
        get_session(context)["selected_product"] = product
        await query.edit_message_text(
            f"Выбрано: {product['name']}\nНа 100 г: У {fmt(product['carbs'])} | Б {fmt(product['proteins'])} | Ж {fmt(product['fats'])}\nТеперь напиши граммовку, например 35",
        )
        return WAITING_GRAMS
    if data == "back:categories":
        await query.edit_message_text("Категории:", reply_markup=categories_inline())
        return CHOOSING
    return CHOOSING


async def find_product_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Напиши название продукта, например: хлеб, банан, плов", reply_markup=ReplyKeyboardRemove())
    context.user_data["search_mode"] = True
    return CHOOSING


async def process_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    user_id = update.effective_user.id

    if text == "❓ Помощь":
        return await help_cmd(update, context)
    if text == "📜 История":
        return await show_history(update, context)
    if text == "⚙️ Настройки":
        return await settings_handler(update, context)
    if text == "♻️ Сброс":
        return await reset_cmd(update, context)
    if text == "↩️ Назад":
        await update.message.reply_text("Вернул в меню.", reply_markup=MAIN_MENU)
        return CHOOSING
    if text in MEAL_NAMES:
        return await choose_meal(update, context)
    if text == "🍞 Быстрые продукты":
        return await choose_quick_products(update, context)
    if text == "📚 Категории":
        return await choose_categories(update, context)
    if text == "🔎 Найти продукт":
        return await find_product_prompt(update, context)
    if text == "📷 Фото еды":
        await update.message.reply_text("Пришли фото тарелки или еды одним сообщением. Я попробую распознать продукты и граммы.", reply_markup=ReplyKeyboardRemove())
        return WAITING_PHOTO
    if text == "✅ Рассчитать":
        session = get_session(context)
        if not session["items"]:
            await update.message.reply_text("Сначала добавь хотя бы один продукт.", reply_markup=MEAL_MENU)
            return CHOOSING
        await update.message.reply_text(meal_summary(context, user_id), reply_markup=ReplyKeyboardRemove())
        await update.message.reply_text("Теперь напиши сахар перед едой, например 6.4")
        return WAITING_GLUCOSE
    if text == "➕ Свой продукт":
        await update.message.reply_text("Напиши название своего продукта или блюда.", reply_markup=ReplyKeyboardRemove())
        return WAITING_ADD_CUSTOM_NAME
    if text.startswith("💉 Шаг шприца"):
        step = 1.0 if text.endswith("1") else 0.5 if text.endswith("0.5") else 0.1
        update_settings(user_id, injection_step=step)
        await update.message.reply_text(f"Сохранил шаг шприца {fmt(step,2)} ед.", reply_markup=SETTINGS_MENU)
        return CHOOSING
    if text == "🍞 1 ХЕ = 12 г":
        update_settings(user_id, xe_grams=12.0)
        await update.message.reply_text("Теперь 1 ХЕ = 12 г углеводов.", reply_markup=SETTINGS_MENU)
        return CHOOSING
    if text == "🍞 1 ХЕ = 10 г":
        update_settings(user_id, xe_grams=10.0)
        await update.message.reply_text("Теперь 1 ХЕ = 10 г углеводов.", reply_markup=SETTINGS_MENU)
        return CHOOSING

    # Search mode or typed product
    results = search_products(text)
    if results:
        context.user_data["search_mode"] = False
        await update.message.reply_text(
            f"Нашёл варианты по запросу «{text}». Выбери продукт:",
            reply_markup=product_buttons(results),
        )
        return CHOOSING

    await update.message.reply_text("Не понял сообщение. Выбери кнопку или напиши продукт точнее.", reply_markup=MEAL_MENU if get_session(context).get("meal_type") else MAIN_MENU)
    return CHOOSING


async def grams_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip().replace(",", ".")
    product = get_session(context).get("selected_product")
    if not product:
        await update.message.reply_text("Сначала выбери продукт.", reply_markup=MEAL_MENU)
        return CHOOSING
    try:
        grams = float(text)
        if grams <= 0:
            raise ValueError
    except Exception:
        await update.message.reply_text("Напиши граммовку числом, например 35")
        return WAITING_GRAMS
    portion = add_item(context, product, grams)
    get_session(context)["selected_product"] = None
    settings = get_settings(update.effective_user.id)
    await update.message.reply_text(portion_text(portion, settings["xe_grams"]), reply_markup=MEAL_MENU)
    await update.message.reply_text(meal_summary(context, update.effective_user.id))
    return CHOOSING


async def glucose_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip().replace(",", ".")
    try:
        glucose = float(text)
        if glucose <= 0:
            raise ValueError
    except Exception:
        await update.message.reply_text("Напиши сахар числом, например 6.4")
        return WAITING_GLUCOSE
    get_session(context)["glucose"] = glucose
    await update.message.reply_text("Теперь напиши суточный инсулин за сегодня, например 17")
    return WAITING_TDD


async def tdd_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip().replace(",", ".")
    try:
        tdd = float(text)
        if tdd <= 0:
            raise ValueError
    except Exception:
        await update.message.reply_text("Напиши суточный инсулин числом, например 17")
        return WAITING_TDD
    session = get_session(context)
    session["tdd"] = tdd
    user_id = update.effective_user.id
    settings = get_settings(user_id)
    totals = meal_totals(session["items"], settings["xe_grams"])
    ins_xe = insulin_per_xe_from_tdd(tdd, settings["xe_grams"])
    exact = totals["xe"] * ins_xe
    rounded = round_step(exact, settings["injection_step"], settings["rounding_mode"])
    icr = icr_from_tdd(tdd)
    lines = [meal_summary(context, user_id)]
    lines.append("")
    lines.append("🧮 Расчёт дозы")
    lines.append(f"500 / {fmt(tdd,2)} = {fmt(icr,2)} г углеводов на 1 ед")
    lines.append(f"{fmt(settings['xe_grams'],2)} / {fmt(icr,2)} = {fmt(ins_xe,2)} ед на 1 ХЕ")
    lines.append(f"{fmt(totals['xe'],2)} ХЕ × {fmt(ins_xe,2)} = {fmt(exact,2)} ед")
    lines.append(f"С учётом шага шприца {fmt(settings['injection_step'],2)} ед: {fmt(rounded,2)} ед")
    if session.get("glucose") is not None and session["glucose"] < 4.0:
        lines.append("⚠️ Сахар низкий. Не полагайся только на расчёт бота, сначала оцени ситуацию вручную.")
    lines.append("⚠️ Это вспомогательный расчёт. Перед уколом обязательно проверь вручную.")
    await update.message.reply_text("\n".join(lines), reply_markup=MAIN_MENU)
    save_history(user_id, session["meal_type"] or "Приём пищи", session["items"], totals["xe"], totals["carbs"], totals["proteins"], totals["fats"], tdd, float(session.get("glucose") or 0), exact, rounded)
    reset_session(context)
    return CHOOSING


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message.photo:
        await update.message.reply_text("Пришли именно фото еды.")
        return WAITING_PHOTO
    try:
        largest = update.message.photo[-1]
        tg_file = await context.bot.get_file(largest.file_id)
        data = await tg_file.download_as_bytearray()
        with get_conn() as conn:
            rows = conn.execute("SELECT name FROM products ORDER BY name").fetchall()
        catalog_names = [r["name"] for r in rows]
        analysis = analyze_photo_with_openai(bytes(data), catalog_names)
        items = analysis.get("items", [])
        if not items:
            await update.message.reply_text("Не смог понять фото. Лучше добавь продукты вручную.", reply_markup=MEAL_MENU)
            return CHOOSING
        matched_lines = ["📷 Нашёл на фото:"]
        settings = get_settings(update.effective_user.id)
        added = 0
        for raw in items:
            name = str(raw.get("name", "")).strip()
            grams = float(raw.get("grams", 0) or 0)
            if grams <= 0 or not name:
                continue
            candidates = search_products(name, limit=1)
            if not candidates:
                matched_lines.append(f"• {name} — {fmt(grams)} г, но такого продукта нет в базе")
                continue
            product = candidates[0]
            portion = add_item(context, product, grams, source="photo", note=str(raw.get("reason", "")))
            added += 1
            matched_lines.append(
                f"• {portion.name} {fmt(portion.grams)} г — У {fmt(portion.carbs)} / Б {fmt(portion.proteins)} / Ж {fmt(portion.fats)} / ХЕ {fmt(xe_for_carbs(portion.carbs, settings['xe_grams']), 2)}"
            )
        if not added:
            await update.message.reply_text("Фото обработал, но в базу ничего не сопоставилось. Добавь продукты вручную.", reply_markup=MEAL_MENU)
            return CHOOSING
        note = analysis.get("note")
        if note:
            matched_lines.append(f"Примечание: {note}")
        matched_lines.append("")
        matched_lines.append("Проверь фото-оценку. Если всё верно, жми ✅ Рассчитать или добавь ещё продукты.")
        await update.message.reply_text("\n".join(matched_lines), reply_markup=MEAL_MENU)
        await update.message.reply_text(meal_summary(context, update.effective_user.id))
        return CHOOSING
    except Exception as e:
        logger.exception("photo analyze error")
        await update.message.reply_text(
            f"Не получилось обработать фото. Проверь, что задан OPENAI_API_KEY, и попробуй ещё раз.\nТехническая ошибка: {str(e)[:180]}",
            reply_markup=MEAL_MENU,
        )
        return CHOOSING


async def custom_name_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["custom_name"] = update.message.text.strip()
    await update.message.reply_text("Углеводы на 100 г? Напиши число, например 24")
    return WAITING_ADD_CUSTOM_C


async def custom_c_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["custom_c"] = float(update.message.text.strip().replace(",", "."))
    except Exception:
        await update.message.reply_text("Напиши число, например 24")
        return WAITING_ADD_CUSTOM_C
    await update.message.reply_text("Белки на 100 г? Напиши число")
    return WAITING_ADD_CUSTOM_P


async def custom_p_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        context.user_data["custom_p"] = float(update.message.text.strip().replace(",", "."))
    except Exception:
        await update.message.reply_text("Напиши число")
        return WAITING_ADD_CUSTOM_P
    await update.message.reply_text("Жиры на 100 г? Напиши число")
    return WAITING_ADD_CUSTOM_F


async def custom_f_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        fats = float(update.message.text.strip().replace(",", "."))
    except Exception:
        await update.message.reply_text("Напиши число")
        return WAITING_ADD_CUSTOM_F
    name = context.user_data.pop("custom_name")
    carbs = context.user_data.pop("custom_c")
    proteins = context.user_data.pop("custom_p")
    product_id = add_custom_product(name, carbs, proteins, fats)
    product = get_product_by_id(product_id)
    get_session(context)["selected_product"] = product
    await update.message.reply_text(
        f"Сохранил продукт {name}. Теперь напиши его граммовку.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return WAITING_GRAMS


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Остановлено.", reply_markup=MAIN_MENU)
    reset_session(context)
    return ConversationHandler.END


def build_app() -> Application:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("Не задан BOT_TOKEN")

    application = ApplicationBuilder().token(token).build()
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CHOOSING: [
                CallbackQueryHandler(callback_router),
                MessageHandler(filters.TEXT & ~filters.COMMAND, process_text),
            ],
            WAITING_GRAMS: [MessageHandler(filters.TEXT & ~filters.COMMAND, grams_handler)],
            WAITING_GLUCOSE: [MessageHandler(filters.TEXT & ~filters.COMMAND, glucose_handler)],
            WAITING_TDD: [MessageHandler(filters.TEXT & ~filters.COMMAND, tdd_handler)],
            WAITING_PHOTO: [MessageHandler(filters.PHOTO, photo_handler), MessageHandler(filters.TEXT & ~filters.COMMAND, process_text)],
            WAITING_ADD_CUSTOM_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, custom_name_handler)],
            WAITING_ADD_CUSTOM_C: [MessageHandler(filters.TEXT & ~filters.COMMAND, custom_c_handler)],
            WAITING_ADD_CUSTOM_P: [MessageHandler(filters.TEXT & ~filters.COMMAND, custom_p_handler)],
            WAITING_ADD_CUSTOM_F: [MessageHandler(filters.TEXT & ~filters.COMMAND, custom_f_handler)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )
    application.add_handler(conv)
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("reset", reset_cmd))
    return application


import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running")

    def log_message(self, format, *args):
        return

def run_web():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), Handler)
    server.serve_forever()

if __name__ == "__main__":
    init_db()

    # Запуск маленького HTTP-сервера для Render
    threading.Thread(target=run_web, daemon=True).start()

    # Создаём и запускаем бота
    app = build_app()
    print("Bot started")

    # Запуск в polling режиме
    app.run_polling()
