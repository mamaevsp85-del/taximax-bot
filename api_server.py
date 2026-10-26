import asyncio
import json
import logging
import os
from datetime import datetime
from aiohttp import web, WSMsgType

from config import settings
from core import storage
from core.router import Router
from core import geocoder
from core.tariff_manager import calculate_trip_price, get_tariff_settings
from max_api.client import MaxClient
from max_api.models import Update
from handlers.common import register_common
from handlers.passenger_new import register_passenger
from handlers.driver import register_driver
from handlers.admin import register_admin

storage.storage = storage.Storage()

client = MaxClient()
router = Router(client)
register_common(router)
register_passenger(router)
register_driver(router)
register_admin(router)

logger = logging.getLogger("api_server")

ws_connections = set()
_geo_cache = {}
_route_cache = {}


@web.middleware
async def auth_middleware(request, handler):
    path = request.path
    if path == '/' or path.startswith('/assets/') or path.startswith('/css/') or path.startswith('/js/'):
        return await handler(request)
    if path in ('/health', '/se14566736_bot', '/ws'):
        return await handler(request)
    if '.' in path.split('/')[-1] and not path.startswith('/api/'):
        return await handler(request)
    api_key = request.headers.get('X-API-Key') or request.query.get('api_key')
    if not api_key or api_key != settings.api_key:
        return web.json_response({'error': 'Unauthorized'}, status=401)
    return await handler(request)


async def health_check(request):
    return web.json_response({'status': 'healthy', 'bot': 'taxi_max', 'mode': 'silent'})


async def geo_geocode(request):
    try:
        data = await request.json()
        address = data.get('address', '')
        if not address:
            return web.json_response({'error': 'address required'}, status=400)
        if address in _geo_cache:
            return web.json_response(_geo_cache[address])
        coords, city = await geocoder.geocode_address(address)
        result = {'address': address, 'coords': coords, 'city': city, 'success': coords is not None}
        _geo_cache[address] = result
        return web.json_response(result)
    except Exception as exc:
        logger.exception("Geocode error")
        return web.json_response({'error': str(exc)}, status=500)

async def geo_route(request):
    try:
        data = await request.json()
        from_coords = data.get('from', {})
        to_coords = data.get('to', {})
        if not from_coords or not to_coords:
            return web.json_response({'error': 'from and to required'}, status=400)
        import math, httpx
        try:
            async with httpx.AsyncClient(trust_env=False) as hc:
                params = {'apikey': settings.yandex_api_key, 'waypoints': f"{from_coords['lon']},{from_coords['lat']}|{to_coords['lon']},{to_coords['lat']}", 'mode': 'driving'}
                resp = await hc.get(settings.yandex_router_url, params=params, timeout=10.0)
                resp.raise_for_status()
                rd = resp.json()
                route = rd.get('route', {})
                result = {'from': from_coords, 'to': to_coords, 'distance': route.get('distance', 0), 'duration': route.get('duration', 0), 'polyline': route.get('polyline', ''), 'success': True}
        except Exception:
            lat1, lon1 = from_coords['lat'], from_coords['lon']
            lat2, lon2 = to_coords['lat'], to_coords['lon']
            R = 6371
            dlat = math.radians(lat2 - lat1)
            dlon = math.radians(lon2 - lon1)
            a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
            c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
            distance = R * c * 1000
            duration = distance / 1000 / 40 * 3600
            result = {'from': from_coords, 'to': to_coords, 'distance': distance, 'duration': duration, 'polyline': '', 'success': True, 'fallback': True}
        return web.json_response(result)
    except Exception as exc:
        logger.exception("Route error")
        return web.json_response({'error': str(exc)}, status=500)


async def orders_price(request):
    try:
        data = await request.json()
        pickup = data.get('pickup', '')
        destination = data.get('destination', '')
        pickup_coords, pickup_city = await geocoder.geocode_address(pickup)
        dest_coords, dest_city = await geocoder.geocode_address(destination)
        if not pickup_coords or not dest_coords:
            return web.json_response({'error': 'Could not geocode addresses'}, status=400)
        import math
        lat1, lon1 = pickup_coords['lat'], pickup_coords['lon']
        lat2, lon2 = dest_coords['lat'], dest_coords['lon']
        R = 6371
        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)
        a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
        distance = R * c
        duration = distance / 40 * 60
        tariff = get_tariff_settings(pickup_city)
        price = calculate_trip_price(distance=distance, duration=duration, pickup_city=pickup_city, destination_city=dest_city)
        return web.json_response({'pickup': pickup, 'destination': destination, 'pickup_coords': pickup_coords, 'dest_coords': dest_coords, 'pickup_city': pickup_city, 'dest_city': dest_city, 'distance': distance, 'duration': duration, 'price': price, 'tariff': tariff, 'success': True})
    except Exception as exc:
        logger.exception("Price error")
        return web.json_response({'error': str(exc)}, status=500)

async def orders_create(request):
    try:
        data = await request.json()
        passenger_id = data.get('passenger_id')
        pickup = data.get('pickup', '')
        destination = data.get('destination', '')
        tariff = data.get('tariff', 'standard')
        if not passenger_id or not pickup or not destination:
            return web.json_response({'error': 'passenger_id, pickup, destination required'}, status=400)
        storage.storage.ensure_user(passenger_id, 'passenger')
        pickup_coords, pickup_city = await geocoder.geocode_address(pickup)
        dest_coords, dest_city = await geocoder.geocode_address(destination)
        import math
        if pickup_coords and dest_coords:
            lat1, lon1 = pickup_coords['lat'], pickup_coords['lon']
            lat2, lon2 = dest_coords['lat'], dest_coords['lon']
            R = 6371
            dlat = math.radians(lat2 - lat1)
            dlon = math.radians(lon2 - lon1)
            a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
            c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
            distance = R * c
            duration = distance / 40 * 60
        else:
            distance, duration = 0, 0
        t = get_tariff_settings(pickup_city)
        price = calculate_trip_price(distance=distance, duration=duration, pickup_city=pickup_city, destination_city=dest_city)
        order = storage.storage.create_order(passenger_id=passenger_id, pickup=pickup, destination=destination, price=price, tariff=tariff)
        await broadcast_update('new_order', order)
        return web.json_response({'success': True, 'order': order})
    except Exception as exc:
        logger.exception("Order creation error")
        return web.json_response({'error': str(exc)}, status=500)


async def orders_list(request):
    try:
        passenger_id = request.query.get('passenger_id')
        driver_id = request.query.get('driver_id')
        status = request.query.get('status')
        orders = []
        if passenger_id:
            orders = storage.storage.get_orders_by_passenger(int(passenger_id))
        elif driver_id:
            orders = storage.storage.get_orders_by_driver(int(driver_id)) if hasattr(storage.storage, 'get_orders_by_driver') else []
        else:
            orders = list(storage.storage.orders.values()) if hasattr(storage.storage, 'orders') else []
        if status:
            orders = [o for o in orders if o.get('status') == status]
        return web.json_response({'orders': orders, 'count': len(orders)})
    except Exception as exc:
        logger.exception("Orders list error")
        return web.json_response({'error': str(exc)}, status=500)


async def drivers_online(request):
    try:
        drivers = storage.storage.get_online_drivers() if hasattr(storage.storage, 'get_online_drivers') else []
        return web.json_response({'drivers': drivers, 'count': len(drivers)})
    except Exception as exc:
        logger.exception("Drivers online error")
        return web.json_response({'error': str(exc)}, status=500)


async def tariffs_list(request):
    try:
        city = request.query.get('city', '')
        tariffs = get_tariff_settings(city) if city else get_tariff_settings()
        return web.json_response({'tariffs': tariffs})
    except Exception as exc:
        logger.exception("Tariffs error")
        return web.json_response({'error': str(exc)}, status=500)


async def users_get(request):
    try:
        user_id = int(request.match_info['user_id'])
        user = storage.storage.get_user(user_id)
        if not user:
            return web.json_response({'error': 'User not found'}, status=404)
        return web.json_response({'user': user})
    except Exception as exc:
        logger.exception("User get error")
        return web.json_response({'error': str(exc)}, status=500)


async def users_create(request):
    try:
        data = await request.json()
        user_id = data.get('user_id')
        role = data.get('role', 'passenger')
        if not user_id:
            return web.json_response({'error': 'user_id required'}, status=400)
        storage.storage.ensure_user(user_id, role)
        user = storage.storage.get_user(user_id)
        return web.json_response({'success': True, 'user': user})
    except Exception as exc:
        logger.exception("User create error")
        return web.json_response({'error': str(exc)}, status=500)

async def websocket_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    ws_connections.add(ws)
    logger.info("WS connected. Total: %d", len(ws_connections))
    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    msg_type = data.get('type')
                    if msg_type == 'ping':
                        await ws.send_json({'type': 'pong'})
                    elif msg_type == 'subscribe':
                        await ws.send_json({'type': 'subscribed', 'events': data.get('events', [])})
                    else:
                        await ws.send_json({'type': 'echo', 'data': data})
                except json.JSONDecodeError:
                    await ws.send_str(msg.data)
            elif msg.type == WSMsgType.ERROR:
                logger.error("WS error: %s", ws.exception())
    finally:
        ws_connections.discard(ws)
        logger.info("WS disconnected. Total: %d", len(ws_connections))
    return ws


async def broadcast_update(event_type, data):
    if not ws_connections:
        return
    message = json.dumps({'type': event_type, 'data': data, 'timestamp': datetime.now().isoformat()})
    for ws in list(ws_connections):
        try:
            await ws.send_str(message)
        except Exception as exc:
            logger.error("WS send failed: %s", exc)
            ws_connections.discard(ws)


async def webhook_handler(request):
    try:
        data = await request.json()
        upd = Update.from_dict(data)
        await router.dispatch(upd)
        return web.json_response({'status': 'ok'})
    except Exception as exc:
        logger.exception("Webhook error")
        return web.json_response({'status': 'error'}, status=500)


async def serve_frontend(request):
    try:
        path = request.path
        if path == '/':
            path = '/index.html'
        frontend_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'frontend')
        file_path = os.path.normpath(os.path.join(frontend_dir, path.lstrip('/')))
        if not file_path.startswith(frontend_dir) or not os.path.isfile(file_path):
            return web.Response(status=404, text='Not found: ' + file_path)
        ext = os.path.splitext(file_path)[1].lower()
        content_types = {
            '.html': 'text/html',
            '.css': 'text/css',
            '.js': 'text/javascript',
            '.jpg': 'image/jpeg',
            '.jpeg': 'image/jpeg',
            '.png': 'image/png',
            '.gif': 'image/gif',
            '.svg': 'image/svg+xml',
            '.ico': 'image/x-icon',
            '.woff': 'font/woff',
            '.woff2': 'font/woff2',
            '.ttf': 'font/ttf',
            '.json': 'application/json',
        }
        content_type = content_types.get(ext, 'application/octet-stream')
        with open(file_path, 'rb') as f:
        return web.Response(body=f.read(), content_type=content_type, charset='utf-8')
    except Exception as exc:
        logger.exception("Serve frontend error")
        return web.Response(status=500, text=f'Error: {exc}')


def create_app():
    app = web.Application(middlewares=[auth_middleware])
    app.router.add_get('/health', health_check)
    app.router.add_post('/api/geo/geocode', geo_geocode)
    app.router.add_post('/api/geo/route', geo_route)
    app.router.add_post('/api/orders/price', orders_price)
    app.router.add_post('/api/orders/create', orders_create)
    app.router.add_get('/api/orders', orders_list)
    app.router.add_get('/api/drivers/online', drivers_online)
    app.router.add_get('/api/tariffs', tariffs_list)
    app.router.add_get('/api/users/{user_id}', users_get)
    app.router.add_post('/api/users', users_create)
    app.router.add_get('/ws', websocket_handler)
    app.router.add_post('/se14566736_bot', webhook_handler)
    app.router.add_get('/{path:.*}', serve_frontend)
    return app


async def main():
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get('PORT', 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logger.info("API Server running at http://0.0.0.0:%d", port)
    logger.info("WebSocket at ws://0.0.0.0:%d/ws", port)
    await asyncio.Event().wait()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())

