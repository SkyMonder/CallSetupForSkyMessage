# server.py
from flask import Flask, request, jsonify
from flask_socketio import SocketIO, emit, join_room, leave_room
import json
import os
import base64
from datetime import datetime
import threading

app = Flask(__name__)
app.config['SECRET_KEY'] = 'skymessage_secret'
socketio = SocketIO(app, cors_allowed_origins="*")

DATA_DIR = 'server_data'
USERS_FILE = os.path.join(DATA_DIR, 'users.json')
GROUPS_FILE = os.path.join(DATA_DIR, 'groups.json')
CHANNELS_FILE = os.path.join(DATA_DIR, 'channels.json')
MESSAGES_FILE = os.path.join(DATA_DIR, 'messages.json')
CALLS_FILE = os.path.join(DATA_DIR, 'calls.json')

# Создаём папку и файлы если их нет
def init_data():
    if not os.path.exists(DATA_DIR):
        os.makedirs(DATA_DIR)
    for file in [USERS_FILE, GROUPS_FILE, CHANNELS_FILE, MESSAGES_FILE, CALLS_FILE]:
        if not os.path.exists(file):
            with open(file, 'w', encoding='utf-8') as f:
                json.dump({}, f)

init_data()

def load_json(file):
    with open(file, 'r', encoding='utf-8') as f:
        return json.load(f)

def save_json(file, data):
    with open(file, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ---------------------------------- Аутентификация ----------------------------------
@app.route('/register', methods=['POST'])
def register():
    data = request.json
    login = data.get('login')
    password = data.get('password')
    avatar = data.get('avatar')  # base64 строка

    users = load_json(USERS_FILE)
    if login in users:
        return jsonify({'success': False, 'error': 'Логин уже существует'}), 400
    users[login] = {
        'password': password,
        'avatar': avatar,
        'contacts': [],
        'groups': [],
        'channels': []
    }
    save_json(USERS_FILE, users)
    return jsonify({'success': True})

@app.route('/login', methods=['POST'])
def login():
    data = request.json
    login = data.get('login')
    password = data.get('password')
    users = load_json(USERS_FILE)
    if login not in users or users[login]['password'] != password:
        return jsonify({'success': False, 'error': 'Неверный логин или пароль'}), 401
    return jsonify({'success': True, 'avatar': users[login].get('avatar')})

# ---------------------------------- Получение данных ----------------------------------
@app.route('/users/<login>')
def get_user(login):
    users = load_json(USERS_FILE)
    if login not in users:
        return jsonify({'error': 'User not found'}), 404
    return jsonify({
        'login': login,
        'avatar': users[login].get('avatar')
    })

@app.route('/search_users')
def search_users():
    query = request.args.get('q', '')
    users = load_json(USERS_FILE)
    result = [{'login': u, 'avatar': users[u].get('avatar')} for u in users if query.lower() in u.lower()]
    return jsonify(result)

@app.route('/groups')
def get_groups():
    groups = load_json(GROUPS_FILE)
    return jsonify(groups)

@app.route('/channels')
def get_channels():
    channels = load_json(CHANNELS_FILE)
    return jsonify(channels)

# ---------------------------------- Сокетные события ----------------------------------
@socketio.on('connect')
def handle_connect():
    print('Client connected')

@socketio.on('disconnect')
def handle_disconnect():
    print('Client disconnected')

@socketio.on('join')
def handle_join(data):
    user = data['user']
    join_room(user)
    emit('joined', {'user': user}, room=user)

@socketio.on('private_message')
def handle_private_message(data):
    to_user = data['to']
    msg = {
        'from': data['from'],
        'text': data['text'],
        'timestamp': datetime.now().isoformat(),
        'type': 'private'
    }
    # Сохраняем сообщение
    messages = load_json(MESSAGES_FILE)
    chat_id = f"private_{data['from']}_{to_user}" if data['from'] < to_user else f"private_{to_user}_{data['from']}"
    if chat_id not in messages:
        messages[chat_id] = []
    messages[chat_id].append(msg)
    save_json(MESSAGES_FILE, messages)
    # Отправляем получателю
    emit('new_message', msg, room=to_user)
    # Отправителю для отображения
    emit('new_message', msg, room=data['from'])

@socketio.on('group_message')
def handle_group_message(data):
    group_id = data['group_id']
    msg = {
        'from': data['from'],
        'text': data['text'],
        'timestamp': datetime.now().isoformat(),
        'type': 'group',
        'group_id': group_id
    }
    # Сохраняем
    messages = load_json(MESSAGES_FILE)
    if group_id not in messages:
        messages[group_id] = []
    messages[group_id].append(msg)
    save_json(MESSAGES_FILE, messages)
    # Отправляем всем участникам группы
    groups = load_json(GROUPS_FILE)
    if group_id in groups:
        for member in groups[group_id]['members']:
            emit('new_message', msg, room=member)

@socketio.on('channel_message')
def handle_channel_message(data):
    channel_id = data['channel_id']
    msg = {
        'from': data['from'],
        'text': data['text'],
        'timestamp': datetime.now().isoformat(),
        'type': 'channel',
        'channel_id': channel_id
    }
    messages = load_json(MESSAGES_FILE)
    if channel_id not in messages:
        messages[channel_id] = []
    messages[channel_id].append(msg)
    save_json(MESSAGES_FILE, messages)
    # Отправляем всем подписчикам канала
    channels = load_json(CHANNELS_FILE)
    if channel_id in channels:
        for member in channels[channel_id]['subscribers']:
            emit('new_message', msg, room=member)

@socketio.on('create_group')
def handle_create_group(data):
    group_name = data['name']
    creator = data['creator']
    members = data.get('members', [])
    members.append(creator)  # создатель тоже участник
    groups = load_json(GROUPS_FILE)
    group_id = f"group_{len(groups)+1}_{group_name}"
    groups[group_id] = {
        'name': group_name,
        'creator': creator,
        'members': list(set(members)),
        'avatar': data.get('avatar')
    }
    save_json(GROUPS_FILE, groups)
    # Обновляем у пользователей список групп
    users = load_json(USERS_FILE)
    for member in groups[group_id]['members']:
        if member in users:
            if group_id not in users[member].get('groups', []):
                users[member].setdefault('groups', []).append(group_id)
    save_json(USERS_FILE, users)
    emit('group_created', {'group_id': group_id, 'group': groups[group_id]}, broadcast=True)

@socketio.on('create_channel')
def handle_create_channel(data):
    channel_name = data['name']
    creator = data['creator']
    channels = load_json(CHANNELS_FILE)
    channel_id = f"channel_{len(channels)+1}_{channel_name}"
    channels[channel_id] = {
        'name': channel_name,
        'creator': creator,
        'subscribers': [creator],
        'avatar': data.get('avatar')
    }
    save_json(CHANNELS_FILE, channels)
    users = load_json(USERS_FILE)
    if creator in users:
        users[creator].setdefault('channels', []).append(channel_id)
    save_json(USERS_FILE, users)
    emit('channel_created', {'channel_id': channel_id, 'channel': channels[channel_id]}, broadcast=True)

@socketio.on('join_channel')
def handle_join_channel(data):
    user = data['user']
    channel_id = data['channel_id']
    channels = load_json(CHANNELS_FILE)
    if channel_id in channels and user not in channels[channel_id]['subscribers']:
        channels[channel_id]['subscribers'].append(user)
        save_json(CHANNELS_FILE, channels)
        users = load_json(USERS_FILE)
        users[user].setdefault('channels', []).append(channel_id)
        save_json(USERS_FILE, users)
        emit('channel_updated', {'channel_id': channel_id, 'subscribers': channels[channel_id]['subscribers']}, broadcast=True)

@socketio.on('add_group_member')
def handle_add_group_member(data):
    group_id = data['group_id']
    new_member = data['member']
    groups = load_json(GROUPS_FILE)
    if group_id in groups and new_member not in groups[group_id]['members']:
        groups[group_id]['members'].append(new_member)
        save_json(GROUPS_FILE, groups)
        users = load_json(USERS_FILE)
        if new_member in users:
            users[new_member].setdefault('groups', []).append(group_id)
        save_json(USERS_FILE, users)
        emit('group_updated', {'group_id': group_id, 'members': groups[group_id]['members']}, broadcast=True)

@socketio.on('remove_group_member')
def handle_remove_group_member(data):
    group_id = data['group_id']
    member = data['member']
    groups = load_json(GROUPS_FILE)
    if group_id in groups and member in groups[group_id]['members']:
        groups[group_id]['members'].remove(member)
        save_json(GROUPS_FILE, groups)
        users = load_json(USERS_FILE)
        if member in users and group_id in users[member].get('groups', []):
            users[member]['groups'].remove(group_id)
        save_json(USERS_FILE, users)
        emit('group_updated', {'group_id': group_id, 'members': groups[group_id]['members']}, broadcast=True)

# ---------------------------------- Звонки (сигнализация) ----------------------------------
@socketio.on('call_user')
def handle_call(data):
    from_user = data['from']
    to_user = data['to']
    # Сохраняем состояние звонка
    calls = load_json(CALLS_FILE)
    call_id = f"{from_user}_{to_user}_{datetime.now().timestamp()}"
    calls[call_id] = {
        'from': from_user,
        'to': to_user,
        'status': 'ringing',
        'start_time': datetime.now().isoformat()
    }
    save_json(CALLS_FILE, calls)
    emit('incoming_call', {'from': from_user, 'call_id': call_id}, room=to_user)

@socketio.on('call_answer')
def handle_call_answer(data):
    call_id = data['call_id']
    answer = data['answer']  # True/False
    calls = load_json(CALLS_FILE)
    if call_id in calls:
        if answer:
            calls[call_id]['status'] = 'active'
            save_json(CALLS_FILE, calls)
            emit('call_started', {'call_id': call_id, 'from': calls[call_id]['from']}, room=calls[call_id]['from'])
            emit('call_started', {'call_id': call_id, 'to': calls[call_id]['to']}, room=calls[call_id]['to'])
        else:
            calls[call_id]['status'] = 'rejected'
            save_json(CALLS_FILE, calls)
            emit('call_rejected', {'call_id': call_id}, room=calls[call_id]['from'])

@socketio.on('call_end')
def handle_call_end(data):
    call_id = data['call_id']
    calls = load_json(CALLS_FILE)
    if call_id in calls:
        calls[call_id]['status'] = 'ended'
        save_json(CALLS_FILE, calls)
        emit('call_ended', {'call_id': call_id}, room=calls[call_id]['from'])
        emit('call_ended', {'call_id': call_id}, room=calls[call_id]['to'])

if __name__ == '__main__':
    # Для production на Render добавляем allow_unsafe_werkzeug=True
    socketio.run(app, host='0.0.0.0', port=5000, debug=False, allow_unsafe_werkzeug=True)
