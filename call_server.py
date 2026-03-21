from flask import Flask, request, jsonify
from flask_socketio import SocketIO, emit, join_room, leave_room
from flask_cors import CORS
import sqlite3
import json
import os
import base64
import uuid
from datetime import datetime

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret!'
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*", ping_timeout=60, ping_interval=25)

# ------------------------------
# База данных
# ------------------------------
DB_PATH = 'skymessage.db'

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        login TEXT PRIMARY KEY,
        password TEXT,
        avatar TEXT,
        online INTEGER DEFAULT 0,
        sid TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS chats (
        chat_id TEXT PRIMARY KEY,
        chat_type TEXT,
        name TEXT,
        created_by TEXT,
        created_at TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS chat_members (
        chat_id TEXT,
        login TEXT,
        role TEXT,
        PRIMARY KEY (chat_id, login)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id TEXT,
        sender TEXT,
        text TEXT,
        timestamp TEXT,
        file_data TEXT
    )''')
    conn.commit()
    conn.close()

init_db()

def db_conn():
    return sqlite3.connect(DB_PATH)

# ------------------------------
# Словари для активных звонков
# ------------------------------
calls = {}  # call_id -> {'caller_sid': sid, 'callee_sid': sid, ...}

# ------------------------------
# HTTP маршруты (для health check)
# ------------------------------
@app.route('/healthz', methods=['GET'])
def healthz():
    return "OK", 200

# ------------------------------
# SocketIO события
# ------------------------------
@socketio.on('connect')
def handle_connect():
    print(f"Client connected: {request.sid}")

@socketio.on('disconnect')
def handle_disconnect():
    # Обновляем статус пользователя в БД
    conn = db_conn()
    c = conn.cursor()
    c.execute("UPDATE users SET online=0, sid=NULL WHERE sid=?", (request.sid,))
    conn.commit()
    conn.close()
    # Очищаем звонки
    to_delete = [cid for cid, call in calls.items() if call.get('caller_sid') == request.sid or call.get('callee_sid') == request.sid]
    for cid in to_delete:
        del calls[cid]
    print(f"Client disconnected: {request.sid}")

@socketio.on('register')
def handle_register(data):
    login = data['login']
    password = data['password']
    conn = db_conn()
    c = conn.cursor()
    try:
        c.execute("INSERT INTO users (login, password) VALUES (?, ?)", (login, password))
        conn.commit()
        emit('register_response', {'status': 'ok'})
    except sqlite3.IntegrityError:
        emit('register_response', {'status': 'error', 'reason': 'Логин уже существует'})
    conn.close()

@socketio.on('login')
def handle_login(data):
    login = data['login']
    password = data['password']
    conn = db_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE login=? AND password=?", (login, password))
    user = c.fetchone()
    if user:
        c.execute("UPDATE users SET online=1, sid=? WHERE login=?", (request.sid, login))
        conn.commit()
        emit('login_response', {'status': 'ok', 'login': login})
        # Отправить список чатов
        c.execute("SELECT chat_id, chat_type, name FROM chats WHERE chat_id IN (SELECT chat_id FROM chat_members WHERE login=?)", (login,))
        rows = c.fetchall()
        chats = []
        for chat_id, chat_type, name in rows:
            if chat_type == 'private':
                c2 = conn.cursor()
                c2.execute("SELECT login FROM chat_members WHERE chat_id=? AND login!=?", (chat_id, login))
                other = c2.fetchone()
                name = other[0] if other else chat_id
            chats.append({'chat_id': chat_id, 'type': chat_type, 'name': name})
        emit('chats_list', {'chats': chats})
    else:
        emit('login_response', {'status': 'error', 'reason': 'Неверный логин или пароль'})
    conn.close()

@socketio.on('search_users')
def handle_search_users(data):
    query = data.get('query', '')
    conn = db_conn()
    c = conn.cursor()
    c.execute("SELECT login FROM users WHERE login LIKE ?", ('%'+query+'%',))
    users = [row[0] for row in c.fetchall()]
    emit('search_users_response', {'users': users})
    conn.close()

@socketio.on('create_chat')
def handle_create_chat(data):
    chat_id = data['chat_id']
    chat_type = data['chat_type']
    name = data.get('name', '')
    created_by = data['created_by']
    members = data['members']
    conn = db_conn()
    c = conn.cursor()
    try:
        c.execute("INSERT INTO chats (chat_id, chat_type, name, created_by, created_at) VALUES (?,?,?,?,?)",
                  (chat_id, chat_type, name, created_by, datetime.now().isoformat()))
        for m in members:
            role = 'owner' if m == created_by else 'member'
            c.execute("INSERT INTO chat_members (chat_id, login, role) VALUES (?,?,?)",
                      (chat_id, m, role))
        conn.commit()
        emit('create_chat_response', {'status': 'ok'})
    except Exception as e:
        emit('create_chat_response', {'status': 'error', 'reason': str(e)})
    conn.close()

@socketio.on('get_messages')
def handle_get_messages(data):
    chat_id = data['chat_id']
    conn = db_conn()
    c = conn.cursor()
    c.execute("SELECT sender, text, timestamp, file_data FROM messages WHERE chat_id=? ORDER BY id", (chat_id,))
    rows = c.fetchall()
    messages = [{'sender': r[0], 'text': r[1], 'timestamp': r[2], 'file': r[3]} for r in rows]
    emit('messages_list', {'messages': messages})
    conn.close()

@socketio.on('send_message')
def handle_send_message(data):
    chat_id = data['chat_id']
    sender = data['sender']
    text = data.get('text', '')
    file_data = data.get('file_data')
    conn = db_conn()
    c = conn.cursor()
    c.execute("INSERT INTO messages (chat_id, sender, text, timestamp, file_data) VALUES (?,?,?,?,?)",
              (chat_id, sender, text, datetime.now().isoformat(), file_data))
    conn.commit()
    # Получить всех участников чата
    c.execute("SELECT login FROM chat_members WHERE chat_id=?", (chat_id,))
    members = [row[0] for row in c.fetchall()]
    conn.close()
    # Отправить сообщение всем онлайн участникам
    for m in members:
        # Получить sid пользователя из БД
        conn2 = db_conn()
        c2 = conn2.cursor()
        c2.execute("SELECT sid FROM users WHERE login=? AND online=1", (m,))
        row = c2.fetchone()
        conn2.close()
        if row:
            sid = row[0]
            socketio.emit('new_message', {
                'chat_id': chat_id,
                'sender': sender,
                'text': text,
                'file': file_data
            }, room=sid)
    emit('send_message_response', {'status': 'ok'})

@socketio.on('upload_avatar')
def handle_upload_avatar(data):
    login = data['login']
    avatar_b64 = data['avatar']
    conn = db_conn()
    c = conn.cursor()
    c.execute("UPDATE users SET avatar=? WHERE login=?", (avatar_b64, login))
    conn.commit()
    conn.close()
    emit('upload_avatar_response', {'status': 'ok'})

@socketio.on('get_avatar')
def handle_get_avatar(data):
    target = data['login']
    conn = db_conn()
    c = conn.cursor()
    c.execute("SELECT avatar FROM users WHERE login=?", (target,))
    row = c.fetchone()
    avatar = row[0] if row else None
    emit('avatar_data', {'avatar': avatar})
    conn.close()

# ------------------------------
# Звонки (сигналы)
# ------------------------------
@socketio.on('call_start')
def handle_call_start(data):
    call_id = data['call_id']
    target = data['target']
    caller_name = data['caller_name']
    # Найти sid получателя
    conn = db_conn()
    c = conn.cursor()
    c.execute("SELECT sid FROM users WHERE login=? AND online=1", (target,))
    row = c.fetchone()
    conn.close()
    if not row:
        emit('call_error', {'reason': 'User offline'})
        return
    callee_sid = row[0]
    calls[call_id] = {
        'caller_sid': request.sid,
        'callee_sid': callee_sid,
        'caller_login': caller_name,
        'callee_login': target,
    }
    socketio.emit('incoming_call', {
        'call_id': call_id,
        'caller_name': caller_name,
        'from': caller_name
    }, room=callee_sid)
    emit('call_start_response', {'status': 'ok'})

@socketio.on('call_accept')
def handle_call_accept(data):
    call_id = data['call_id']
    call = calls.get(call_id)
    if call and call['callee_sid'] == request.sid:
        socketio.emit('call_accepted', {'call_id': call_id}, room=call['caller_sid'])
        emit('call_accept_response', {'status': 'ok'})
    else:
        emit('call_error', {'reason': 'Not callee or call not found'})

@socketio.on('call_reject')
def handle_call_reject(data):
    call_id = data['call_id']
    call = calls.get(call_id)
    if call and call['callee_sid'] == request.sid:
        socketio.emit('call_rejected', {'call_id': call_id}, room=call['caller_sid'])
        del calls[call_id]
        emit('call_reject_response', {'status': 'ok'})
    else:
        emit('call_error', {'reason': 'Not callee or call not found'})

@socketio.on('call_end')
def handle_call_end(data):
    call_id = data['call_id']
    call = calls.get(call_id)
    if call:
        if call['caller_sid'] == request.sid or call['callee_sid'] == request.sid:
            other = call['callee_sid'] if call['caller_sid'] == request.sid else call['caller_sid']
            if other:
                socketio.emit('call_ended', {'call_id': call_id}, room=other)
            del calls[call_id]
            emit('call_end_response', {'status': 'ok'})
        else:
            emit('call_error', {'reason': 'Not participant'})
    else:
        emit('call_error', {'reason': 'Call not found'})

@socketio.on('webrtc_signal')
def handle_webrtc_signal(data):
    call_id = data['call_id']
    signal = data['signal']
    call = calls.get(call_id)
    if call:
        target_sid = call['callee_sid'] if request.sid == call['caller_sid'] else call['caller_sid']
        if target_sid:
            socketio.emit('webrtc_signal', {
                'call_id': call_id,
                'signal': signal
            }, room=target_sid)
            emit('webrtc_signal_response', {'status': 'ok'})
        else:
            emit('call_error', {'reason': 'Target not found'})
    else:
        emit('call_error', {'reason': 'Call not found'})

@socketio.on('logout')
def handle_logout():
    # Уже обрабатывается в disconnect, но можно явно
    pass

# ------------------------------
# Запуск
# ------------------------------
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8000))
    socketio.run(app, host='0.0.0.0', port=port)
