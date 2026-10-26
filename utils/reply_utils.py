"""Utility functions for Mini App - converted from ui/reply.py logic."""
from typing import Optional, List, Dict, Any, Tuple
from datetime import datetime


def make_profile_text(user: dict) -> str:
    """Возвращает текст профиля пассажира.
    
    Args:
        user: Словарь с данными пользователя из storage
        
    Returns:
        Текст сообщения для отображения в Mini App
    """
    name = user.get('name') or '-'
    phone = user.get('phone') or '-'
    rating = user.get('rating', 5.0)
    rides = user.get('orders_completed', 0)
    cancelled = user.get('orders_cancelled', 0)
    
    return f"""👤 <b>Профиль пассажира</b>

Имя: {name}
Телефон: {phone}
Рейтинг: {rating}/5
Поездок выполнено: {rides}
Отменено: {cancelled}"""


def make_history_text(orders: list, page: int = 0, page_size: int = 5) -> Dict[str, Any]:
    """Возвращает структуру данных истории заказов.
    
    Args:
        orders: Список заказов из storage
        page: Номер страницы (0-indexed)
        page_size: Заказов на странице
        
    Returns:
        Dict с текстом, клавиатурой и пагинацией
    """
    # Фильтруем завершенные/отмененные заказы
    completed = [o for o in orders if o.get('status') in ('completed', 'cancelled')]
    total = len(completed)
    
    # Пагинация
    total_pages = (total + page_size - 1) // page_size if total > 0 else 1
    page = max(0, min(page, total_pages - 1)) if total > 0 else 0
    start = page * page_size
    end = start + page_size
    page_orders = completed[start:end]
    
    # Строим текст
    status_icons = {"completed": "✅", "cancelled": "❌"}
    lines = [f"📜 <b>История заказов</b> (стр. {page + 1}/{total_pages})"]
    
    for o in page_orders:
        icon = status_icons.get(o.get('status', ''), '•')
        from_city = o.get('from_city', '-')
        to_city = o.get('to_city', '-')
        price = o.get('price', 0)
        lines.append(f"{icon} #{o.get('id', '-')} {from_city} → {to_city} ({price}₽)")
    
    # Клавиатура (простая передача callback_data)
    keyboard = None
    if total > 0:
        keyboard = [
            [{"text": "↪️ Главное меню", "callback_data": "passenger:menu"}],
        ]
    
    return {
        "text": "\n".join(lines),
        "keyboard": keyboard,
        "page": page,
        "total_pages": total_pages,
        "total_orders": total
    }


def make_welcome_text() -> str:
    """Возвравет приветственный текст Mini App."""
    return """🚗 Добро пожаловать в ТаксиМакс!

Выберите действие:"""


def make_role_selection_text() -> str:
    """Возвравет текст выбора роли."""
    return """👤 Выберите роль:"""


def make_main_menu_text() -> str:
    """Возвравет текст главного меню пассажира."""
    return """🚗 Добро пожаловать в такси!

Выберите действие:"""


def extract_callback_data(text: str) -> Optional[Tuple[str, str]]:
    """Парсит текст сообщения на наличие callback_data.
    
    Returns:
        Tuple (section, action) или None
    """
    # Ищем стандартные паттерны
    import re
    
    patterns = [
        r'passenger:(\w+)',       # passenger:menu, passenger:profile
        r'menu:(\w+)',            # menu:roles
        r'role:(\w+)',            # role:passenger
    ]
    
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            section = match.group(0).rsplit(':', 1)[0]
            action = match.group(1)
            return (section, action)
    
    return None