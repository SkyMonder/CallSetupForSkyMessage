from flask import Flask, request, jsonify
from flask_socketio import SocketIO, emit, join_room, leave_room
import sqlite3
from datetime import datetime
import json
import os

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret!'
socketio = SocketIO(app, cors_allowed_origins="*")

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
        online INTEGER DEFAULT 0
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
# Хранилище активных сокетов (sid -> login)
# ------------------------------
clients = {}  # sid -> login
user_sids = {}  # login -> sid
calls = {}  # call_id -> {'caller_sid': sid, 'callee_sid': sid}

# ------------------------------
# HTTP endpoints
# ------------------------------
@app.route('/healthz')
def healthz():
    return 'OK', 200

@app.route('/')
def index():
    return 'SkyMessage server is running', 200

# ------------------------------
# SocketIO обработчики
# ------------------------------
@socketio.on('register')
def handle_register(data):
    login = data['login']
    password = data['password']
    conn = db_conn()
    c = conn.cursor()
    try:
        c.execute("INSERT INTO users (login, password) VALUES (?, ?)", (login, password))
        conn.commit()
        emit('registered', {'status': 'ok'})
    except sqlite3.IntegrityError:
        emit('error', {'message': 'Логин уже существует'})
    conn.close()

@socketio.on('login')
def handle_login(data):
    login = data['login']
    password = data['password']
    conn = db_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE login=? AND password=?", (login, password))
    if c.fetchone():
        c.execute("UPDATE users SET online=1 WHERE login=?", (login,))
        conn.commit()
        # Сохраняем связь sid <-> login
        clients[request.sid] = login
        user_sids[login] = request.sid
        emit('logged_in', {'status': 'ok', 'login': login})
    else:
        emit('error', {'message': 'Неверный логин или пароль'})
    conn.close()

@socketio.on('search_users')
def handle_search_users(data):
    query = data.get('query', '')
    conn = db_conn()
    c = conn.cursor()
    c.execute("SELECT login FROM users WHERE login LIKE ?", ('%'+query+'%',))
    users = [row[0] for row in c.fetchall()]
    emit('search_results', {'users': users})
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
    c.execute("INSERT INTO chats (chat_id, chat_type, name, created_by, created_at) VALUES (?,?,?,?,?)",
              (chat_id, chat_type, name, created_by, datetime.now().isoformat()))
    for m in members:
        role = 'owner' if m == created_by else 'member'
        c.execute("INSERT INTO chat_members (chat_id, login, role) VALUES (?,?,?)",
                  (chat_id, m, role))
    conn.commit()
    conn.close()
    emit('chat_created', {'status': 'ok'})

@socketio.on('get_chats')
def handle_get_chats():
    login = clients.get(request.sid)
    if not login:
        return
    conn = db_conn()
    c = conn.cursor()
    c.execute("SELECT chat_id, chat_type, name FROM chats WHERE chat_id IN (SELECT chat_id FROM chat_members WHERE login=?)",
              (login,))
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
    conn.close()

@socketio.on('get_messages')
def handle_get_messages(data):
    chat_id = data['chat_id']
    conn = db_conn()
    c = conn.cursor()
    c.execute("SELECT sender, text, timestamp, file_data FROM messages WHERE chat_id=? ORDER BY id", (chat_id,))
    rows = c.fetchall()
    messages = [{'sender': r[0], 'text': r[1], 'timestamp': r[2], 'file': r[3]} for r in rows]
    emit('messages', {'messages': messages})
    conn.close()

@socketio.on('send_message')
def handle_send_message(data):
    login = clients.get(request.sid)
    if not login:
        return
    chat_id = data['chat_id']
    text = data.get('text', '')
    file_data = data.get('file_data')
    conn = db_conn()
    c = conn.cursor()
    c.execute("INSERT INTO messages (chat_id, sender, text, timestamp, file_data) VALUES (?,?,?,?,?)",
              (chat_id, login, text, datetime.now().isoformat(), file_data))
    conn.commit()
    # Получить всех участников чата
    c.execute("SELECT login FROM chat_members WHERE chat_id=?", (chat_id,))
    members = [row[0] for row in c.fetchall()]
    conn.close()
    # Рассылка всем онлайн участникам
    for m in members:
        if m in user_sids:
            socketio.emit('new_message', {
                'chat_id': chat_id,
                'sender': login,
                'text': text,
                'file': file_data
            }, room=user_sids[m])
    emit('message_sent', {'status': 'ok'})

@socketio.on('upload_avatar')
def handle_upload_avatar(data):
    login = clients.get(request.sid)
    if not login:
        return
    avatar_b64 = data['avatar']
    conn = db_conn()
    c = conn.cursor()
    c.execute("UPDATE users SET avatar=? WHERE login=?", (avatar_b64, login))
    conn.commit()
    conn.close()
    emit('avatar_uploaded', {'status': 'ok'})

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
# Звонки (сигнализация через SocketIO)
# ------------------------------
@socketio.on('call_start')
def handle_call_start(data):
    login = clients.get(request.sid)
    if not login:
        return
    call_id = data['call_id']
    target = data['target']
    if target not in user_sids:
        emit('error', {'message': 'Пользователь не в сети'})
        return
    callee_sid = user_sids[target]
    calls[call_id] = {
        'caller_sid': request.sid,
        'callee_sid': callee_sid,
        'caller_login': login,
        'callee_login': target,
    }
    socketio.emit('incoming_call', {
        'call_id': call_id,
        'caller_name': login,
        'from': login
    }, room=callee_sid)
    emit('call_started', {'status': 'ok'})

@socketio.on('call_accept')
def handle_call_accept(data):
    call_id = data['call_id']
    if call_id not in calls:
        return
    call = calls[call_id]
    if request.sid != call['callee_sid']:
        return
    socketio.emit('call_accepted', {'call_id': call_id}, room=call['caller_sid'])
    emit('call_accepted', {'call_id': call_id})

@socketio.on('call_reject')
def handle_call_reject(data):
    call_id = data['call_id']
    if call_id not in calls:
        return
    call = calls[call_id]
    if request.sid != call['callee_sid']:
        return
    socketio.emit('call_rejected', {'call_id': call_id}, room=call['caller_sid'])
    del calls[call_id]
    emit('call_rejected', {'call_id': call_id})

@socketio.on('call_end')
def handle_call_end(data):
    call_id = data['call_id']
    if call_id not in calls:
        return
    call = calls[call_id]
    # Уведомляем обоих
    socketio.emit('call_ended', {'call_id': call_id}, room=call['caller_sid'])
    if call['callee_sid']:
        socketio.emit('call_ended', {'call_id': call_id}, room=call['callee_sid'])
    del calls[call_id]
    emit('call_ended', {'call_id': call_id})

@socketio.on('webrtc_signal')
def handle_webrtc_signal(data):
    call_id = data['call_id']
    signal = data['signal']
    if call_id not in calls:
        return
    call = calls[call_id]
    target_sid = call['callee_sid'] if request.sid == call['caller_sid'] else call['caller_sid']
    if target_sid:
        socketio.emit('webrtc_signal', {
            'call_id': call_id,
            'signal': signal
        }, room=target_sid)

@socketio.on('disconnect')
def handle_disconnect():
    login = clients.pop(request.sid, None)
    if login:
        user_sids.pop(login, None)
        # Обновить онлайн статус в БД
        conn = db_conn()
        c = conn.cursor()
        c.execute("UPDATE users SET online=0 WHERE login=?", (login,))
        conn.commit()
        conn.close()
        # Завершить звонки с участием этого клиента
        to_delete = []
        for cid, call in calls.items():
            if request.sid in (call['caller_sid'], call['callee_sid']):
                other_sid = call['callee_sid'] if request.sid == call['caller_sid'] else call['caller_sid']
                if other_sid:
                    socketio.emit('call_ended', {'call_id': cid}, room=other_sid)
                to_delete.append(cid)
        for cid in to_delete:
            del calls[cid]

# ------------------------------
# Запуск
# ------------------------------
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8000))
    socketio.run(app, host='0.0.0.0', port=port)
