import asyncio
import json
import os
import signal
from aiohttp import web

# Хранилище активных звонков и пользователей
calls = {}
user_sockets = {}  # login -> WebSocketResponse

async def websocket_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    login = None
    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                data = json.loads(msg.data)
                cmd = data.get('cmd')

                if cmd == 'register':
                    login = data['login']
                    user_sockets[login] = ws
                    await ws.send_json({'type': 'registered', 'status': 'ok'})
                    print(f"User {login} registered")

                elif cmd == 'call_start':
                    call_id = data['call_id']
                    target = data['target']
                    caller_name = data.get('caller_name', login)

                    if target not in user_sockets:
                        await ws.send_json({'type': 'error', 'message': f'User {target} is offline'})
                        continue

                    callee_ws = user_sockets[target]
                    calls[call_id] = {
                        'caller': ws,
                        'callee': callee_ws,
                        'caller_login': login,
                        'callee_login': target,
                        'caller_name': caller_name,
                    }

                    await callee_ws.send_json({
                        'type': 'incoming_call',
                        'call_id': call_id,
                        'caller_name': caller_name,
                        'from': login,
                    })

                elif cmd == 'call_accept':
                    call_id = data['call_id']
                    call = calls.get(call_id)
                    if call and call['callee'] == ws:
                        await call['caller'].send_json({
                            'type': 'call_accepted',
                            'call_id': call_id,
                        })

                elif cmd == 'call_reject':
                    call_id = data['call_id']
                    call = calls.get(call_id)
                    if call and call['callee'] == ws:
                        await call['caller'].send_json({
                            'type': 'call_rejected',
                            'call_id': call_id,
                        })
                        del calls[call_id]

                elif cmd == 'call_end':
                    call_id = data['call_id']
                    call = calls.get(call_id)
                    if call:
                        for sock in (call['caller'], call['callee']):
                            if sock:
                                await sock.send_json({
                                    'type': 'call_ended',
                                    'call_id': call_id,
                                })
                        del calls[call_id]

                elif cmd == 'webrtc_signal':
                    call_id = data['call_id']
                    signal_data = data['signal']
                    call = calls.get(call_id)
                    if call:
                        target_sock = call['callee'] if call['caller'] == ws else call['caller']
                        if target_sock:
                            await target_sock.send_json({
                                'type': 'webrtc_signal',
                                'call_id': call_id,
                                'signal': signal_data,
                            })
            elif msg.type == web.WSMsgType.ERROR:
                print('WebSocket connection closed with exception')
    except Exception as e:
        print(f"WebSocket error: {e}")
    finally:
        if login and login in user_sockets:
            del user_sockets[login]
        # Очистить звонки, в которых участвовал этот клиент
        for cid, call in list(calls.items()):
            if ws in (call['caller'], call['callee']):
                other = call['callee'] if call['caller'] == ws else call['caller']
                if other:
                    try:
                        await other.send_json({'type': 'call_ended', 'call_id': cid})
                    except:
                        pass
                del calls[cid]
    return ws

async def health_check(request):
    """Обработка GET /healthz"""
    return web.Response(text='OK')

async def on_shutdown(app):
    # При завершении закрываем все WebSocket-соединения
    for ws in user_sockets.values():
        await ws.close()

app = web.Application()
app.router.add_get('/healthz', health_check)
app.router.add_get('/ws', websocket_handler)   # WebSocket endpoint
app.on_shutdown.append(on_shutdown)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', '8000'))
    web.run_app(app, port=port)
