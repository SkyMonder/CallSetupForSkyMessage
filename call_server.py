"""
Call Server for SkyMessage - сигнальный сервер для WebRTC звонков.
Поддерживает масштабирование через Redis (если установлен redis).
Развертывается на Render.
"""

import os
import json
import redis
from flask import Flask, request
from flask_socketio import SocketIO, emit, join_room, leave_room

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'your-secret-key')

# Используем Redis если настроен, иначе работаем в одном процессе
redis_url = os.environ.get('REDIS_URL')
if redis_url:
    # Настройка для масштабирования
    redis_client = redis.from_url(redis_url)
    socketio = SocketIO(app, cors_allowed_origins="*", message_queue=redis_url)
else:
    socketio = SocketIO(app, cors_allowed_origins="*")

# Хранилище активных пользователей: login -> sid (будет синхронизироваться через Redis)
# Для простоты используем словарь в памяти, но при нескольких инстансах нужно общее хранилище.
# Если Redis есть, будем хранить в Redis.
user_sids = {}  # локальный кэш, но при масштабировании нужно использовать Redis.

def get_user_sid(login):
    if redis_url:
        return redis_client.get(f"user:{login}")
    else:
        return user_sids.get(login)

def set_user_sid(login, sid):
    if redis_url:
        redis_client.setex(f"user:{login}", 3600, sid)  # храним час
    else:
        user_sids[login] = sid

def delete_user_sid(login):
    if redis_url:
        redis_client.delete(f"user:{login}")
    else:
        if login in user_sids:
            del user_sids[login]

# Хранилище активных звонков
calls = {}  # call_id -> info
# Если используем Redis, можно хранить и calls в Redis, но для простоты оставим в памяти.
# Для масштабирования нужно будет также синхронизировать calls. Но так как сигнальный сервер обычно stateless,
# можно хранить состояние звонков в Redis. Для примера реализуем базовый вариант.

@app.route('/health')
def health():
    return "OK", 200

@socketio.on('connect')
def handle_connect():
    print(f"Client connected: {request.sid}")

@socketio.on('register')
def handle_register(data):
    login = data.get('login')
    if login:
        set_user_sid(login, request.sid)
        # Присоединяем к комнате по логину для удобства
        join_room(login)
        print(f"User {login} registered with sid {request.sid}")
        emit('registered', {'status': 'ok'})
    else:
        emit('error', {'message': 'No login provided'})

@socketio.on('disconnect')
def handle_disconnect():
    # Удаляем пользователя из хранилища
    # Находим логин по sid (это неэффективно, но для простоты)
    login_to_remove = None
    if redis_url:
        # Сканируем ключи user:* (не рекомендуется, но для демо)
        for key in redis_client.scan_iter("user:*"):
            if redis_client.get(key) == request.sid.encode():
                login_to_remove = key.decode().split(':')[1]
                break
    else:
        for login, sid in list(user_sids.items()):
            if sid == request.sid:
                login_to_remove = login
                break
    if login_to_remove:
        delete_user_sid(login_to_remove)
        leave_room(login_to_remove)
        print(f"User {login_to_remove} disconnected")
    # Также очищаем звонки, где участвовал этот sid
    for call_id, call in list(calls.items()):
        if call.get('caller') == request.sid or call.get('callee') == request.sid:
            # Уведомить другого участника
            other = call['callee'] if call['caller'] == request.sid else call['caller']
            if other:
                socketio.emit('call_ended', {'call_id': call_id}, room=other)
            del calls[call_id]

@socketio.on('call_start')
def handle_call_start(data):
    call_id = data['call_id']
    target_login = data['target_user']
    caller_name = data.get('caller_name', '')
    caller_sid = request.sid

    # Найти SID получателя
    callee_sid = get_user_sid(target_login)
    if not callee_sid:
        emit('call_error', {'reason': f'User {target_login} is offline'})
        return

    # Сохраняем информацию о звонке
    calls[call_id] = {
        'caller': caller_sid,
        'callee': callee_sid,
        'caller_name': caller_name,
        'status': 'ringing'
    }
    # Отправляем уведомление о входящем звонке
    socketio.emit('incoming_call', {
        'call_id': call_id,
        'caller_name': caller_name,
        'from_sid': caller_sid
    }, room=callee_sid)

@socketio.on('call_accept')
def handle_call_accept(data):
    call_id = data['call_id']
    call = calls.get(call_id)
    if not call:
        emit('call_error', {'reason': 'Call not found'})
        return
    if call['callee'] != request.sid:
        emit('call_error', {'reason': 'Not your call'})
        return
    call['status'] = 'active'
    # Уведомляем вызывающего
    socketio.emit('call_accepted', {'call_id': call_id}, room=call['caller'])

@socketio.on('call_reject')
def handle_call_reject(data):
    call_id = data['call_id']
    call = calls.get(call_id)
    if call and call['callee'] == request.sid:
        # Уведомляем вызывающего
        socketio.emit('call_rejected', {'call_id': call_id}, room=call['caller'])
        del calls[call_id]

@socketio.on('call_hangup')
def handle_call_hangup(data):
    call_id = data['call_id']
    call = calls.get(call_id)
    if call:
        # Уведомляем другого участника
        other = call['callee'] if call['caller'] == request.sid else call['caller']
        if other:
            socketio.emit('call_ended', {'call_id': call_id}, room=other)
        del calls[call_id]

# Передача WebRTC сигналов (offer, answer, ice-candidate)
@socketio.on('webrtc_offer')
def handle_webrtc_offer(data):
    call_id = data['call_id']
    offer = data['offer']
    call = calls.get(call_id)
    if call and call['caller'] == request.sid:
        # Пересылаем offer получателю
        socketio.emit('webrtc_offer', {
            'call_id': call_id,
            'offer': offer
        }, room=call['callee'])

@socketio.on('webrtc_answer')
def handle_webrtc_answer(data):
    call_id = data['call_id']
    answer = data['answer']
    call = calls.get(call_id)
    if call and call['callee'] == request.sid:
        socketio.emit('webrtc_answer', {
            'call_id': call_id,
            'answer': answer
        }, room=call['caller'])

@socketio.on('webrtc_ice')
def handle_webrtc_ice(data):
    call_id = data['call_id']
    candidate = data['candidate']
    call = calls.get(call_id)
    if call:
        # Отправить другому участнику
        other = call['callee'] if call['caller'] == request.sid else call['caller']
        if other:
            socketio.emit('webrtc_ice', {
                'call_id': call_id,
                'candidate': candidate
            }, room=other)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port, debug=False)
