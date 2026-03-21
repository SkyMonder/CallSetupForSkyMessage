from flask import Flask, request
from flask_socketio import SocketIO, emit
import sqlite3
import json
import os
from datetime import datetime
import uuid

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret!'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

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

clients = {}
calls = {}

def send_response(sid, data, callback_id=None):
    if callback_id:
        data['callback_id'] = callback_id
    socketio.emit('registered' if data.get('status') == 'ok' and 'login' not in data else 
                  'logged_in' if data.get('status') == 'ok' and 'login' in data else
                  'search_result' if 'users' in data else
                  'chat_created' if data.get('status') == 'ok' and 'chat_id' in data else
                  'avatar_uploaded' if data.get('status') == 'ok' and 'avatar' in data else
                  'avatar_data' if 'avatar' in data else
                  'error', data, room=sid)

@socketio.on('register')
def handle_register(data):
    callback_id = data.get('callback_id')
    login = data['login']
    password = data['password']
    conn = db_conn()
    c = conn.cursor()
    try:
        c.execute("INSERT INTO users (login, password) VALUES (?, ?)", (login, password))
        conn.commit()
        send_response(request.sid, {'status': 'ok'}, callback_id)
    except sqlite3.IntegrityError:
        send_response(request.sid, {'status': 'error', 'reason': 'Логин уже существует'}, callback_id)
    conn.close()

@socketio.on('login')
def handle_login(data):
    callback_id = data.get('callback_id')
    login = data['login']
    password = data['password']
    conn = db_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE login=? AND password=?", (login, password))
    if c.fetchone():
        c.execute("UPDATE users SET online=1 WHERE login=?", (login,))
        conn.commit()
        clients[login] = request.sid
        send_response(request.sid, {'status': 'ok', 'login': login}, callback_id)
    else:
        send_response(request.sid, {'status': 'error', 'reason': 'Неверный логин или пароль'}, callback_id)
    conn.close()

@socketio.on('search_users')
def handle_search_users(data):
    callback_id = data.get('callback_id')
    query = data.get('query', '')
    conn = db_conn()
    c = conn.cursor()
    c.execute("SELECT login FROM users WHERE login LIKE ?", ('%'+query+'%',))
    users = [row[0] for row in c.fetchall()]
    send_response(request.sid, {'users': users}, callback_id)
    conn.close()

@socketio.on('create_chat')
def handle_create_chat(data):
    callback_id = data.get('callback_id')
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
    send_response(request.sid, {'status': 'ok', 'chat_id': chat_id}, callback_id)

@socketio.on('get_chats')
def handle_get_chats(data):
    login = None
    for l, sid in clients.items():
        if sid == request.sid:
            login = l
            break
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
    socketio.emit('chats_list', chats, room=request.sid)
    conn.close()

@socketio.on('get_messages')
def handle_get_messages(data):
    chat_id = data['chat_id']
    conn = db_conn()
    c = conn.cursor()
    c.execute("SELECT sender, text, timestamp, file_data FROM messages WHERE chat_id=? ORDER BY id", (chat_id,))
    rows = c.fetchall()
    messages = [{'sender': r[0], 'text': r[1], 'timestamp': r[2], 'file': r[3]} for r in rows]
    socketio.emit('messages', messages, room=request.sid)
    conn.close()

@socketio.on('send_message')
def handle_send_message(data):
    login = None
    for l, sid in clients.items():
        if sid == request.sid:
            login = l
            break
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
    c.execute("SELECT login FROM chat_members WHERE chat_id=?", (chat_id,))
    members = [row[0] for row in c.fetchall()]
    conn.close()
    for m in members:
        if m in clients:
            socketio.emit('new_message', {
                'chat_id': chat_id,
                'sender': login,
                'text': text,
                'file': file_data
            }, room=clients[m])
    socketio.emit('message_sent', {'status': 'ok'}, room=request.sid)

@socketio.on('upload_avatar')
def handle_upload_avatar(data):
    callback_id = data.get('callback_id')
    login = None
    for l, sid in clients.items():
        if sid == request.sid:
            login = l
            break
    if not login:
        return
    avatar_b64 = data['avatar']
    conn = db_conn()
    c = conn.cursor()
    c.execute("UPDATE users SET avatar=? WHERE login=?", (avatar_b64, login))
    conn.commit()
    conn.close()
    send_response(request.sid, {'status': 'ok'}, callback_id)

@socketio.on('get_avatar')
def handle_get_avatar(data):
    callback_id = data.get('callback_id')
    target = data['login']
    conn = db_conn()
    c = conn.cursor()
    c.execute("SELECT avatar FROM users WHERE login=?", (target,))
    row = c.fetchone()
    avatar = row[0] if row else None
    send_response(request.sid, {'avatar': avatar}, callback_id)
    conn.close()

@socketio.on('call_start')
def handle_call_start(data):
    call_id = data['call_id']
    target = data['target']
    if target not in clients:
        send_response(request.sid, {'status': 'error', 'reason': 'User offline'})
        return
    calls[call_id] = {
        'caller': request.sid,
        'callee': clients[target],
        'caller_login': None,  # можно получить по sid
        'callee_login': target,
    }
    socketio.emit('incoming_call', {
        'call_id': call_id,
        'caller_name': next((l for l, sid in clients.items() if sid == request.sid), ''),
        'from': next((l for l, sid in clients.items() if sid == request.sid), '')
    }, room=clients[target])
    send_response(request.sid, {'status': 'ok'})

@socketio.on('call_accept')
def handle_call_accept(data):
    call_id = data['call_id']
    if call_id in calls:
        call = calls[call_id]
        if call['callee'] == request.sid:
            socketio.emit('call_accepted', {'call_id': call_id}, room=call['caller'])
            send_response(request.sid, {'status': 'ok'})
        else:
            send_response(request.sid, {'status': 'error', 'reason': 'Not callee'})
    else:
        send_response(request.sid, {'status': 'error', 'reason': 'Call not found'})

@socketio.on('call_reject')
def handle_call_reject(data):
    call_id = data['call_id']
    if call_id in calls:
        call = calls[call_id]
        if call['callee'] == request.sid:
            socketio.emit('call_rejected', {'call_id': call_id}, room=call['caller'])
            del calls[call_id]
            send_response(request.sid, {'status': 'ok'})
        else:
            send_response(request.sid, {'status': 'error', 'reason': 'Not callee'})
    else:
        send_response(request.sid, {'status': 'error', 'reason': 'Call not found'})

@socketio.on('call_end')
def handle_call_end(data):
    call_id = data['call_id']
    if call_id in calls:
        call = calls[call_id]
        for room in (call['caller'], call['callee']):
            if room:
                socketio.emit('call_ended', {'call_id': call_id}, room=room)
        del calls[call_id]
        send_response(request.sid, {'status': 'ok'})
    else:
        send_response(request.sid, {'status': 'error', 'reason': 'Call not found'})

@socketio.on('webrtc_signal')
def handle_webrtc_signal(data):
    call_id = data['call_id']
    signal = data['signal']
    if call_id in calls:
        call = calls[call_id]
        target_sid = call['callee'] if call['caller'] == request.sid else call['caller']
        if target_sid:
            socketio.emit('webrtc_signal', {'call_id': call_id, 'signal': signal}, room=target_sid)
            send_response(request.sid, {'status': 'ok'})
        else:
            send_response(request.sid, {'status': 'error', 'reason': 'Target not found'})
    else:
        send_response(request.sid, {'status': 'error', 'reason': 'Call not found'})

@socketio.on('disconnect')
def handle_disconnect():
    # Удалить клиента из clients
    for login, sid in list(clients.items()):
        if sid == request.sid:
            del clients[login]
            break
    # Удалить звонки с участием этого клиента
    for cid, call in list(calls.items()):
        if call['caller'] == request.sid or call['callee'] == request.sid:
            other = call['callee'] if call['caller'] == request.sid else call['caller']
            if other:
                socketio.emit('call_ended', {'call_id': cid}, room=other)
            del calls[cid]

@app.route('/healthz')
def healthz():
    return 'OK', 200

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port)
