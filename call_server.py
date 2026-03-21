import asyncio
import json
import os
import sqlite3
from datetime import datetime
import websockets
from websockets.asyncio.server import serve

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

async def handler(websocket):
    login = None
    try:
        async for message in websocket:
            data = json.loads(message)
            cmd = data.get('cmd')

            if cmd == 'register':
                login = data['login']
                pwd = data['password']
                conn = db_conn()
                c = conn.cursor()
                try:
                    c.execute("INSERT INTO users (login, password) VALUES (?, ?)", (login, pwd))
                    conn.commit()
                    await websocket.send(json.dumps({'status': 'ok'}))
                except sqlite3.IntegrityError:
                    await websocket.send(json.dumps({'status': 'error', 'reason': 'Логин уже существует'}))
                conn.close()

            elif cmd == 'login':
                login = data['login']
                pwd = data['password']
                conn = db_conn()
                c = conn.cursor()
                c.execute("SELECT * FROM users WHERE login=? AND password=?", (login, pwd))
                if c.fetchone():
                    c.execute("UPDATE users SET online=1 WHERE login=?", (login,))
                    conn.commit()
                    clients[login] = websocket
                    await websocket.send(json.dumps({'status': 'ok', 'login': login}))
                else:
                    await websocket.send(json.dumps({'status': 'error', 'reason': 'Неверный логин или пароль'}))
                conn.close()

            elif cmd == 'search_users':
                query = data.get('query', '')
                conn = db_conn()
                c = conn.cursor()
                c.execute("SELECT login FROM users WHERE login LIKE ?", ('%'+query+'%',))
                users = [row[0] for row in c.fetchall()]
                await websocket.send(json.dumps({'users': users}))
                conn.close()

            elif cmd == 'create_chat':
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
                await websocket.send(json.dumps({'status': 'ok'}))

            elif cmd == 'get_chats':
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
                await websocket.send(json.dumps({'type': 'chats_list', 'chats': chats}))
                conn.close()

            elif cmd == 'get_messages':
                chat_id = data['chat_id']
                conn = db_conn()
                c = conn.cursor()
                c.execute("SELECT sender, text, timestamp, file_data FROM messages WHERE chat_id=? ORDER BY id", (chat_id,))
                rows = c.fetchall()
                messages = [{'sender': r[0], 'text': r[1], 'timestamp': r[2], 'file': r[3]} for r in rows]
                await websocket.send(json.dumps({'type': 'messages', 'messages': messages}))
                conn.close()

            elif cmd == 'send_message':
                chat_id = data['chat_id']
                sender = login
                text = data.get('text', '')
                file_data = data.get('file_data')
                conn = db_conn()
                c = conn.cursor()
                c.execute("INSERT INTO messages (chat_id, sender, text, timestamp, file_data) VALUES (?,?,?,?,?)",
                          (chat_id, sender, text, datetime.now().isoformat(), file_data))
                conn.commit()
                c.execute("SELECT login FROM chat_members WHERE chat_id=?", (chat_id,))
                members = [row[0] for row in c.fetchall()]
                conn.close()
                for m in members:
                    if m in clients:
                        await clients[m].send(json.dumps({
                            'type': 'new_message',
                            'chat_id': chat_id,
                            'sender': sender,
                            'text': text,
                            'file': file_data
                        }))
                await websocket.send(json.dumps({'status': 'ok'}))

            elif cmd == 'upload_avatar':
                avatar_b64 = data['avatar']
                conn = db_conn()
                c = conn.cursor()
                c.execute("UPDATE users SET avatar=? WHERE login=?", (avatar_b64, login))
                conn.commit()
                conn.close()
                await websocket.send(json.dumps({'status': 'ok'}))

            elif cmd == 'get_avatar':
                target = data['login']
                conn = db_conn()
                c = conn.cursor()
                c.execute("SELECT avatar FROM users WHERE login=?", (target,))
                row = c.fetchone()
                avatar = row[0] if row else None
                await websocket.send(json.dumps({'avatar': avatar}))
                conn.close()

            elif cmd == 'call_start':
                call_id = data['call_id']
                target = data['target']
                if target not in clients:
                    await websocket.send(json.dumps({'status': 'error', 'reason': 'User offline'}))
                    continue
                calls[call_id] = {
                    'caller': websocket,
                    'callee': clients[target],
                    'caller_login': login,
                    'callee_login': target,
                }
                await clients[target].send(json.dumps({
                    'type': 'incoming_call',
                    'call_id': call_id,
                    'caller_name': login,
                    'from': login
                }))
                await websocket.send(json.dumps({'status': 'ok'}))

            elif cmd == 'call_accept':
                call_id = data['call_id']
                if call_id in calls:
                    call = calls[call_id]
                    if call['callee'] == websocket:
                        await call['caller'].send(json.dumps({'type': 'call_accepted', 'call_id': call_id}))
                        await websocket.send(json.dumps({'status': 'ok'}))
                    else:
                        await websocket.send(json.dumps({'status': 'error', 'reason': 'Not callee'}))

            elif cmd == 'call_reject':
                call_id = data['call_id']
                if call_id in calls:
                    call = calls[call_id]
                    if call['callee'] == websocket:
                        await call['caller'].send(json.dumps({'type': 'call_rejected', 'call_id': call_id}))
                        del calls[call_id]
                        await websocket.send(json.dumps({'status': 'ok'}))
                    else:
                        await websocket.send(json.dumps({'status': 'error', 'reason': 'Not callee'}))

            elif cmd == 'call_end':
                call_id = data['call_id']
                if call_id in calls:
                    call = calls[call_id]
                    for sock in (call['caller'], call['callee']):
                        if sock:
                            await sock.send(json.dumps({'type': 'call_ended', 'call_id': call_id}))
                    del calls[call_id]
                    await websocket.send(json.dumps({'status': 'ok'}))

            elif cmd == 'webrtc_signal':
                call_id = data['call_id']
                signal = data['signal']
                if call_id in calls:
                    call = calls[call_id]
                    target_sock = call['callee'] if call['caller'] == websocket else call['caller']
                    if target_sock:
                        await target_sock.send(json.dumps({
                            'type': 'webrtc_signal',
                            'call_id': call_id,
                            'signal': signal
                        }))
                    await websocket.send(json.dumps({'status': 'ok'}))

            elif cmd == 'logout':
                if login:
                    conn = db_conn()
                    c = conn.cursor()
                    c.execute("UPDATE users SET online=0 WHERE login=?", (login,))
                    conn.commit()
                    conn.close()
                    if login in clients:
                        del clients[login]
                await websocket.send(json.dumps({'status': 'ok'}))
                break

    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        if login and login in clients:
            del clients[login]
        to_delete = [cid for cid, call in calls.items() if websocket in (call['caller'], call['callee'])]
        for cid in to_delete:
            del calls[cid]

async def main():
    port = int(os.environ.get('PORT', '8000'))
    host = '0.0.0.0'
    async with serve(handler, host, port):
        print(f"Server running on {host}:{port}")
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
