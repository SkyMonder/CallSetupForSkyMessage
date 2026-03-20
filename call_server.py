#!/usr/bin/env python
import asyncio
import http
import json
import os
import signal
import uuid

import websockets
from websockets.asyncio.server import serve

# Хранилище активных звонков
calls = {}  # call_id -> {'caller': websocket, 'callee': websocket, ...}
user_sockets = {}  # login -> websocket


async def error(websocket, message):
    await websocket.send(json.dumps({'type': 'error', 'message': message}))


async def handler(websocket):
    login = None
    try:
        async for message in websocket:
            data = json.loads(message)
            cmd = data.get('cmd')

            if cmd == 'register':
                login = data['login']
                user_sockets[login] = websocket
                await websocket.send(json.dumps({'type': 'registered', 'status': 'ok'}))
                print(f"User {login} registered")

            elif cmd == 'call_start':
                call_id = data['call_id']
                target = data['target']
                caller_name = data.get('caller_name', login)

                if target not in user_sockets:
                    await error(websocket, f'User {target} is offline')
                    continue

                callee_socket = user_sockets[target]
                calls[call_id] = {
                    'caller': websocket,
                    'callee': callee_socket,
                    'caller_login': login,
                    'callee_login': target,
                    'caller_name': caller_name,
                }

                # Отправляем входящий звонок
                await callee_socket.send(json.dumps({
                    'type': 'incoming_call',
                    'call_id': call_id,
                    'caller_name': caller_name,
                    'from': login,
                }))

            elif cmd == 'call_accept':
                call_id = data['call_id']
                call = calls.get(call_id)
                if call and call['callee'] == websocket:
                    # Уведомляем вызывающего
                    await call['caller'].send(json.dumps({
                        'type': 'call_accepted',
                        'call_id': call_id,
                    }))

            elif cmd == 'call_reject':
                call_id = data['call_id']
                call = calls.get(call_id)
                if call and call['callee'] == websocket:
                    await call['caller'].send(json.dumps({
                        'type': 'call_rejected',
                        'call_id': call_id,
                    }))
                    del calls[call_id]

            elif cmd == 'call_end':
                call_id = data['call_id']
                call = calls.get(call_id)
                if call:
                    # Уведомляем обоих участников
                    for sock in (call['caller'], call['callee']):
                        if sock:
                            await sock.send(json.dumps({
                                'type': 'call_ended',
                                'call_id': call_id,
                            }))
                    del calls[call_id]

            elif cmd == 'webrtc_signal':
                # Пересылаем offer/answer/ice другому участнику
                call_id = data['call_id']
                signal_data = data['signal']  # {'type': 'offer', 'sdp': ...}
                call = calls.get(call_id)
                if call:
                    # Отправляем сигнал тому, кому он адресован
                    target_sock = call['callee'] if call['caller'] == websocket else call['caller']
                    if target_sock:
                        await target_sock.send(json.dumps({
                            'type': 'webrtc_signal',
                            'call_id': call_id,
                            'signal': signal_data,
                        }))

    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        # Очистка при отключении
        if login and login in user_sockets:
            del user_sockets[login]
        # Закрываем все звонки, где участвовал этот клиент
        for cid, call in list(calls.items()):
            if websocket in (call['caller'], call['callee']):
                other = call['callee'] if call['caller'] == websocket else call['caller']
                if other:
                    try:
                        await other.send(json.dumps({'type': 'call_ended', 'call_id': cid}))
                    except:
                        pass
                del calls[cid]


async def health_check(request):
    """
    Обработчик HTTP-запросов для health check.
    Поддерживает GET и HEAD на пути /healthz.
    """
    if request.method == 'HEAD':
        # Для HEAD-запросов возвращаем статус без тела
        return http.HTTPStatus.OK, [], b''
    if request.path == '/healthz' and request.method == 'GET':
        return http.HTTPStatus.OK, [], b'OK\n'
    return None


async def main():
    port = int(os.environ.get('PORT', '8000'))
    host = '0.0.0.0'

    async with serve(
        handler,
        host,
        port,
        process_request=health_check,
    ):
        print(f'Server started on {host}:{port}')

        # Ожидаем сигнал завершения
        loop = asyncio.get_running_loop()
        stop = loop.create_future()
        loop.add_signal_handler(signal.SIGTERM, stop.set_result, None)

        await stop


if __name__ == '__main__':
    asyncio.run(main())
