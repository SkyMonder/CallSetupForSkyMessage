# server.py
from flask import Flask, request, jsonify
from flask_socketio import SocketIO, emit, join_room
import json
import os
import base64
from datetime import datetime
import uuid

app = Flask(__name__)
app.config['SECRET_KEY'] = 'skymessage_secret'
socketio = SocketIO(app, cors_allowed_origins="*")

DATA_DIR = 'server_data'
USERS_FILE = os.path.join(DATA_DIR, 'users.json')
GROUPS_FILE = os.path.join(DATA_DIR, 'groups.json')
CHANNELS_FILE = os.path.join(DATA_DIR, 'channels.json')
MESSAGES_FILE = os.path.join(DATA_DIR, 'messages.json')
PROFILES_FILE = os.path.join(DATA_DIR, 'profiles.json')

def init_data():
    if not os.path.exists(DATA_DIR):
        os.makedirs(DATA_DIR)
    for file in [USERS_FILE, GROUPS_FILE, CHANNELS_FILE, MESSAGES_FILE, PROFILES_FILE]:
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
    avatar = data.get('avatar')

    users = load_json(USERS_FILE)
    if login in users:
        return jsonify({'success': False, 'error': 'Логин уже существует'}), 400
    users[login] = {
        'password': password,
        'avatar': avatar,
        'contacts': []  # логины, с которыми были сообщения
    }
    save_json(USERS_FILE, users)
    
    profiles = load_json(PROFILES_FILE)
    profiles[login] = {
        'description': '',
        'avatar': avatar
    }
    save_json(PROFILES_FILE, profiles)
    
    return jsonify({'success': True})

@app.route('/login', methods=['POST'])
def login():
    data = request.json
    login = data.get('login')
    password = data.get('password')
    users = load_json(USERS_FILE)
    if login not in users or users[login]['password'] != password:
        return jsonify({'success': False, 'error': 'Неверный логин или пароль'}), 401
    return jsonify({'success': True})

# ---------------------------------- Профили ----------------------------------
@app.route('/profile/<login>')
def get_profile(login):
    profiles = load_json(PROFILES_FILE)
    if login not in profiles:
        return jsonify({'error': 'Profile not found'}), 404
    return jsonify(profiles[login])

@app.route('/update_profile', methods=['POST'])
def update_profile():
    data = request.json
    login = data.get('login')
    description = data.get('description')
    avatar = data.get('avatar')
    profiles = load_json(PROFILES_FILE)
    if login not in profiles:
        return jsonify({'error': 'User not found'}), 404
    if description is not None:
        profiles[login]['description'] = description
    if avatar is not None:
        profiles[login]['avatar'] = avatar
        # также обновляем в users
        users = load_json(USERS_FILE)
        if login in users:
            users[login]['avatar'] = avatar
            save_json(USERS_FILE, users)
    save_json(PROFILES_FILE, profiles)
    return jsonify({'success': True})

# ---------------------------------- Поиск и контакты ----------------------------------
@app.route('/search_users')
def search_users():
    query = request.args.get('q', '')
    users = load_json(USERS_FILE)
    result = [{'login': u} for u in users if query.lower() in u.lower()]
    return jsonify(result)

@app.route('/contacts/<login>')
def get_contacts(login):
    users = load_json(USERS_FILE)
    if login not in users:
        return jsonify([])
    contacts = users[login].get('contacts', [])
    return jsonify(contacts)

# ---------------------------------- Сообщения ----------------------------------
def save_message(chat_id, msg):
    messages = load_json(MESSAGES_FILE)
    if chat_id not in messages:
        messages[chat_id] = []
    messages[chat_id].append(msg)
    save_json(MESSAGES_FILE, messages)

def add_contact_if_needed(user, other):
    users = load_json(USERS_FILE)
    if user in users and other not in users[user].get('contacts', []):
        users[user].setdefault('contacts', []).append(other)
        save_json(USERS_FILE, users)

@socketio.on('private_message')
def handle_private_message(data):
    to_user = data['to']
    from_user = data['from']
    msg = {
        'id': str(uuid.uuid4()),
        'from': from_user,
        'text': data['text'],
        'timestamp': datetime.now().isoformat(),
        'type': 'private',
        'file': data.get('file'),  # base64 или None
        'file_name': data.get('file_name'),
        'voice': data.get('voice')  # для голосовых
    }
    chat_id = f"private_{from_user}_{to_user}" if from_user < to_user else f"private_{to_user}_{from_user}"
    save_message(chat_id, msg)
    # Добавляем в контакты обоим
    add_contact_if_needed(from_user, to_user)
    add_contact_if_needed(to_user, from_user)
    # Отправляем получателю и отправителю
    emit('new_message', msg, room=to_user)
    emit('new_message', msg, room=from_user)

@socketio.on('group_message')
def handle_group_message(data):
    group_id = data['group_id']
    from_user = data['from']
    msg = {
        'id': str(uuid.uuid4()),
        'from': from_user,
        'text': data['text'],
        'timestamp': datetime.now().isoformat(),
        'type': 'group',
        'group_id': group_id,
        'file': data.get('file'),
        'file_name': data.get('file_name'),
        'voice': data.get('voice')
    }
    save_message(group_id, msg)
    groups = load_json(GROUPS_FILE)
    if group_id in groups:
        for member in groups[group_id]['members']:
            emit('new_message', msg, room=member)

@socketio.on('channel_message')
def handle_channel_message(data):
    channel_id = data['channel_id']
    from_user = data['from']
    msg = {
        'id': str(uuid.uuid4()),
        'from': from_user,
        'text': data['text'],
        'timestamp': datetime.now().isoformat(),
        'type': 'channel',
        'channel_id': channel_id,
        'file': data.get('file'),
        'file_name': data.get('file_name')
    }
    save_message(channel_id, msg)
    channels = load_json(CHANNELS_FILE)
    if channel_id in channels:
        for subscriber in channels[channel_id]['subscribers']:
            emit('new_message', msg, room=subscriber)

@socketio.on('delete_message')
def handle_delete_message(data):
    msg_id = data['msg_id']
    chat_id = data['chat_id']
    messages = load_json(MESSAGES_FILE)
    if chat_id in messages:
        messages[chat_id] = [m for m in messages[chat_id] if m['id'] != msg_id]
        save_json(MESSAGES_FILE, messages)
        emit('message_deleted', {'msg_id': msg_id, 'chat_id': chat_id}, broadcast=True)

@socketio.on('edit_message')
def handle_edit_message(data):
    msg_id = data['msg_id']
    chat_id = data['chat_id']
    new_text = data['new_text']
    messages = load_json(MESSAGES_FILE)
    if chat_id in messages:
        for m in messages[chat_id]:
            if m['id'] == msg_id:
                m['text'] = new_text
                m['edited'] = True
                save_json(MESSAGES_FILE, messages)
                emit('message_edited', {'msg_id': msg_id, 'chat_id': chat_id, 'new_text': new_text}, broadcast=True)
                break

# ---------------------------------- Группы ----------------------------------
@socketio.on('create_group')
def handle_create_group(data):
    name = data['name']
    creator = data['creator']
    members = data.get('members', [])
    members.append(creator)
    groups = load_json(GROUPS_FILE)
    group_id = f"group_{len(groups)+1}_{name}"
    groups[group_id] = {
        'name': name,
        'creator': creator,
        'members': list(set(members)),
        'avatar': data.get('avatar')
    }
    save_json(GROUPS_FILE, groups)
    # Добавляем группы в профили участников
    users = load_json(USERS_FILE)
    for member in groups[group_id]['members']:
        if member in users:
            users[member].setdefault('groups', []).append(group_id)
    save_json(USERS_FILE, users)
    emit('group_created', {'group_id': group_id, 'group': groups[group_id]}, broadcast=True)

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

# ---------------------------------- Каналы ----------------------------------
@socketio.on('create_channel')
def handle_create_channel(data):
    name = data['name']
    creator = data['creator']
    channels = load_json(CHANNELS_FILE)
    channel_id = f"channel_{len(channels)+1}_{name}"
    channels[channel_id] = {
        'name': name,
        'creator': creator,
        'subscribers': [creator],
        'avatar': data.get('avatar')
    }
    save_json(CHANNELS_FILE, channels)
    emit('channel_created', {'channel_id': channel_id, 'channel': channels[channel_id]}, broadcast=True)

@socketio.on('subscribe_channel')
def handle_subscribe_channel(data):
    user = data['user']
    channel_id = data['channel_id']
    channels = load_json(CHANNELS_FILE)
    if channel_id in channels and user not in channels[channel_id]['subscribers']:
        channels[channel_id]['subscribers'].append(user)
        save_json(CHANNELS_FILE, channels)
        emit('channel_updated', {'channel_id': channel_id, 'subscribers': channels[channel_id]['subscribers']}, broadcast=True)

@socketio.on('unsubscribe_channel')
def handle_unsubscribe_channel(data):
    user = data['user']
    channel_id = data['channel_id']
    channels = load_json(CHANNELS_FILE)
    if channel_id in channels and user in channels[channel_id]['subscribers']:
        channels[channel_id]['subscribers'].remove(user)
        save_json(CHANNELS_FILE, channels)
        emit('channel_updated', {'channel_id': channel_id, 'subscribers': channels[channel_id]['subscribers']}, broadcast=True)

# ---------------------------------- Получение данных ----------------------------------
@app.route('/groups')
def get_groups():
    groups = load_json(GROUPS_FILE)
    return jsonify(groups)

@app.route('/channels')
def get_channels():
    channels = load_json(CHANNELS_FILE)
    return jsonify(channels)

@app.route('/messages/<chat_id>')
def get_messages(chat_id):
    messages = load_json(MESSAGES_FILE)
    return jsonify(messages.get(chat_id, []))

# ---------------------------------- Сокетные соединения ----------------------------------
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

if __name__ == '__main__':
    # Для Render используем allow_unsafe_werkzeug=True
    socketio.run(app, host='0.0.0.0', port=5000, allow_unsafe_werkzeug=True)
