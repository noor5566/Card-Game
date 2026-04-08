#!/usr/bin/env python3
"""
Hearts & Spades — Multiplayer Server v2
- Game state survives browser reload
- Full history stored per room
- Pure Python stdlib, no pip needed
Run: python server_v2.py
"""
import socket,threading,json,hashlib,base64,struct,os,sys,time,random,string
import urllib.parse

PORT=8766  # ONE port for HTTP + WebSocket
HOST="0.0.0.0"
SUITS=['H','D','C','S']
RANKS=['2','3','4','5','6','7','8','9','10','J','Q','K','A']
RV={r:i for i,r in enumerate(RANKS)}

def cpts(c):
    if c['s']=='H':return 1
    if c['s']=='S'and c['r']=='Q':return 12
    return 0
def isqs(c):return c['s']=='S'and c['r']=='Q'
def mkdeck():return[{'s':s,'r':r}for s in SUITS for r in RANKS]
def shuf(l):l=l[:];random.shuffle(l);return l
def sorth(h):o={'S':0,'H':1,'D':2,'C':3};h.sort(key=lambda c:(o[c['s']],RV[c['r']]))
def playable(hand,led):
    if not led:return list(range(len(hand)))
    si=[i for i,c in enumerate(hand)if c['s']==led]
    return si if si else list(range(len(hand)))

rooms={}   # code->Room
clients={} # id->Client
lock=threading.Lock()

def gencode():
    while True:
        c=''.join(random.choices(string.ascii_uppercase+string.digits,k=5))
        if c not in rooms:return c

class Room:
    def __init__(self,code,host_id):
        self.code=code;self.host_id=host_id
        self.players={}    # slot->ws_id (None=AI/disconnected)
        self.names={}      # slot->name
        self.tokens={}     # token->slot  (for reconnect)
        self.state=None    # game state dict
        self.history=[]    # list of round result dicts
        self.chat=[]       # last 50 messages
        self.created=time.time()

    def slot_of(self,wid):
        return next((s for s,w in self.players.items()if w==wid),None)
    def ws_of(self,slot):return self.players.get(slot)
    def free_slot(self):
        return next((s for s in range(1,5)if s not in self.players),None)
    def full(self):return len(self.players)>=4
    def pcount(self):return len(self.players)

    def bcast(self,msg,exc=None):
        d=json.dumps(msg)
        for s,wid in list(self.players.items()):
            if wid and wid!=exc:
                cl=clients.get(wid)
                if cl:cl.send(d)

    def lobby(self):
        return{'type':'lobby','code':self.code,
               'players':{str(s):self.names.get(s,'')for s in range(1,5)},
               'count':self.pcount(),'full':self.full(),
               'history':self.history,'chat':self.chat[-20:]}

    def start(self):
        deck=shuf(mkdeck())
        hands={i+1:deck[i*13:(i+1)*13]for i in range(4)}
        for h in hands.values():sorth(h)
        d=random.randint(1,4)
        # Fill AI slots
        for s in range(1,5):
            if s not in self.players:
                self.players[s]=None
                ai_names=['','Aisha','Bilal','Zara','Omar']
                self.names[s]=ai_names[s]+' (AI)'
        self.state={
            'round':1,'dealer':d,'cur':(d%4)+1,'hands':hands,
            'totals':{i:0 for i in range(1,5)},
            'rpts':{i:0 for i in range(1,5)},
            'tricks':{i:0 for i in range(1,5)},
            'ann':{i:False for i in range(1,5)},
            'fail':{i:False for i in range(1,5)},
            'trick':[],'led':None,'qrcvr':None,
            'phase':'announce','ann_done':{},'resolving':False,
        }

    def gstate(self,slot):
        st=self.state
        if not st:return None
        return{
            'type':'game_state','round':st['round'],'dealer':st['dealer'],
            'cur':st['cur'],'phase':st['phase'],
            'hand':st['hands'].get(slot,[]),
            'hand_counts':{str(s):len(st['hands'].get(s,[]))for s in range(1,5)},
            'totals':{str(s):st['totals'][s]for s in range(1,5)},
            'rpts':{str(s):st['rpts'][s]for s in range(1,5)},
            'tricks':{str(s):st['tricks'][s]for s in range(1,5)},
            'ann':{str(s):st['ann'][s]for s in range(1,5)},
            'fail':{str(s):st['fail'][s]for s in range(1,5)},
            'trick':st['trick'],'led':st['led'],
            'names':{str(s):self.names.get(s,'P'+str(s))for s in range(1,5)},
            'my_slot':slot,
            'ann_done':{str(k):v for k,v in st['ann_done'].items()},
            'history':self.history,
            'chat':self.chat[-20:],
        }

    def bcast_gs(self):
        for slot,wid in list(self.players.items()):
            if wid:
                gs=self.gstate(slot)
                if gs:
                    cl=clients.get(wid)
                    if cl:cl.send(json.dumps(gs))

    def do_announce(self,slot,yes):
        st=self.state
        if st['phase']!='announce'or slot in st['ann_done']:return
        st['ann'][slot]=yes;st['ann_done'][slot]=True
        self.bcast({'type':'announce_made','slot':slot,'yes':yes,
                    'name':self.names.get(slot,'P'+str(slot))})
        # Check all done (including AI)
        if len(st['ann_done'])==4:
            st['phase']='play';st['ann_done']={}
            self.bcast_gs()

    def do_play(self,slot,ck):
        st=self.state
        if st['phase']!='play'or st['cur']!=slot or st['resolving']:return
        hand=st['hands'].get(slot,[])
        card=next((c for c in hand if c['r']+c['s']==ck),None)
        if not card:return
        pl=playable(hand,st['led'])
        if hand.index(card)not in pl:return
        hand.remove(card)
        if not st['led']:st['led']=card['s']
        st['trick'].append({'p':slot,'c':card})
        self.bcast({'type':'card_played','slot':slot,
                    'name':self.names.get(slot,'P'+str(slot)),'card':card})
        if len(st['trick'])==4:
            st['resolving']=True
            threading.Timer(0.8,self.resolve).start()
        else:
            st['cur']=(slot%4)+1
            self.bcast_gs()

    def resolve(self):
        st=self.state
        led=st['led']
        lc=[t for t in st['trick']if t['c']['s']==led]
        win=max(lc,key=lambda t:RV[t['c']['r']])
        wp=win['p']
        tp=sum(cpts(t['c'])for t in st['trick'])
        qp=next((t for t in st['trick']if isqs(t['c'])),None)
        st['tricks'][wp]+=1
        if qp:st['qrcvr']=wp
        # Announcement fail
        if st['ann'][wp]and not st['fail'][wp]:
            st['fail'][wp]=True
            for i in range(1,5):st['rpts'][i]=0
            st['rpts'][wp]=50
            nm=self.names.get(wp,'P'+str(wp))
            self.bcast({'type':'trick_result','winner':wp,'pts':tp,'queen':bool(qp),
                'ann_failed':wp,'name':nm,
                'msg':nm+' FAILED announcement! +50 pts — round ends!'})
            st['resolving']=False
            threading.Timer(1.2,self.end_round).start()
            return
        if not st['fail'][wp]:st['rpts'][wp]+=tp
        nm=self.names.get(wp,'P'+str(wp))
        msg=nm+' wins trick (+'+str(tp)+'pts)'+('' if not qp else' ♠ Queen!')
        self.bcast({'type':'trick_result','winner':wp,'pts':tp,
                    'queen':bool(qp),'name':nm,'msg':msg})
        if all(len(h)==0 for h in st['hands'].values()):
            st['resolving']=False
            threading.Timer(1.2,self.end_round).start()
        else:
            st['trick']=[];st['led']=None;st['cur']=wp;st['resolving']=False
            threading.Timer(0.5,self.bcast_gs).start()

    def end_round(self):
        st=self.state
        if not any(st['fail'][i]for i in range(1,5)):
            for i in range(1,5):
                if st['ann'][i]and st['tricks'][i]==0:st['rpts'][i]=-25
                elif st['tricks'][i]==0:st['rpts'][i]=-5
        for i in range(1,5):st['totals'][i]+=st['rpts'][i]
        # Save to history
        rec={'round':st['round'],
             'rpts':{str(s):st['rpts'][s]for s in range(1,5)},
             'totals':{str(s):st['totals'][s]for s in range(1,5)},
             'tricks':{str(s):st['tricks'][s]for s in range(1,5)},
             'ann':{str(s):st['ann'][s]for s in range(1,5)},
             'fail':{str(s):st['fail'][s]for s in range(1,5)},
             'names':{str(s):self.names.get(s,'P'+str(s))for s in range(1,5)}}
        self.history.append(rec)
        elim=[i for i in range(1,5)if st['totals'][i]>=100]
        self.bcast({'type':'round_end',
            'rpts':{str(s):st['rpts'][s]for s in range(1,5)},
            'totals':{str(s):st['totals'][s]for s in range(1,5)},
            'tricks':{str(s):st['tricks'][s]for s in range(1,5)},
            'ann':{str(s):st['ann'][s]for s in range(1,5)},
            'fail':{str(s):st['fail'][s]for s in range(1,5)},
            'elim':elim,'history':self.history})
        if elim:
            non=[i for i in range(1,5)if i not in elim]
            mn=min(st['totals'][i]for i in non)
            winners=[i for i in non if st['totals'][i]==mn]
            self.bcast({'type':'game_over','elim':elim,'winners':winners,
                'totals':{str(s):st['totals'][s]for s in range(1,5)},
                'names':{str(s):self.names.get(s,'P'+str(s))for s in range(1,5)},
                'history':self.history})

    def next_round(self):
        st=self.state
        nd=st['qrcvr']or((st['dealer']%4)+1)
        deck=shuf(mkdeck())
        hands={i+1:deck[i*13:(i+1)*13]for i in range(4)}
        for h in hands.values():sorth(h)
        st.update({'round':st['round']+1,'dealer':nd,'cur':(nd%4)+1,'hands':hands,
            'rpts':{i:0 for i in range(1,5)},'tricks':{i:0 for i in range(1,5)},
            'ann':{i:False for i in range(1,5)},'fail':{i:False for i in range(1,5)},
            'trick':[],'led':None,'qrcvr':None,'phase':'announce',
            'ann_done':{},'resolving':False})
        self.bcast_gs()

    def add_chat(self,slot,text):
        nm=self.names.get(slot,'P'+str(slot))
        msg={'slot':slot,'name':nm,'text':text,'time':int(time.time())}
        self.chat.append(msg)
        if len(self.chat)>200:self.chat=self.chat[-200:]
        self.bcast({'type':'chat','slot':slot,'name':nm,'text':text})
        return msg

# ── WebSocket ────────────────────────────────────────────────
MAGIC="258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
def wshs(conn,key):
    acc=base64.b64encode(hashlib.sha1((key+MAGIC).encode()).digest()).decode()
    conn.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                  "Connection: Upgrade\r\nSec-WebSocket-Accept: "+acc+"\r\n\r\n").encode())
def wsrecv(conn):
    try:
        hdr=b''
        while len(hdr)<2:
            c=conn.recv(2-len(hdr))
            if not c:return None
            hdr+=c
        b1,b2=hdr[0],hdr[1];op=b1&0x0F
        if op==8:return None
        if op==9:wssend(conn,b'',10);return''
        masked=bool(b2&0x80);ln=b2&0x7F
        if ln==126:ln=struct.unpack('>H',conn.recv(2))[0]
        elif ln==127:ln=struct.unpack('>Q',conn.recv(8))[0]
        mask=conn.recv(4)if masked else b'\x00\x00\x00\x00'
        data=b''
        while len(data)<ln:
            c=conn.recv(min(4096,ln-len(data)))
            if not c:return None
            data+=c
        if masked:data=bytes(b^mask[i%4]for i,b in enumerate(data))
        return data.decode('utf-8',errors='replace')
    except:return None
def wssend(conn,data,op=1):
    if isinstance(data,str):data=data.encode()
    ln=len(data)
    hdr=bytes([0x80|op])
    if ln<126:hdr+=bytes([ln])
    elif ln<65536:hdr+=bytes([126])+struct.pack('>H',ln)
    else:hdr+=bytes([127])+struct.pack('>Q',ln)
    try:conn.sendall(hdr+data)
    except:pass

class Client:
    _n=0;_lk=threading.Lock()
    def __init__(self,conn,addr):
        with Client._lk:Client._n+=1;self.id=Client._n
        self.conn=conn;self.addr=addr;self.room=None;self.slot=None
        self.token=None;self.slk=threading.Lock()
    def send(self,t):
        with self.slk:wssend(self.conn,t)
    def close(self):
        try:self.conn.close()
        except:pass

# ── Message Handler ──────────────────────────────────────────
def handle_msg(cl,raw):
    try:msg=json.loads(raw)
    except:return
    t=msg.get('type','')
    with lock:
        if t=='create_room':
            name=msg.get('name','Player')[:20]
            token=msg.get('token','')
            code=gencode()
            room=Room(code,cl.id)
            room.players[1]=cl.id;room.names[1]=name
            tok=''.join(random.choices(string.ascii_letters+string.digits,k=24))
            room.tokens[tok]=1
            rooms[code]=room;cl.room=code;cl.slot=1;cl.token=tok
            cl.send(json.dumps({'type':'joined','slot':1,'code':code,'name':name,'token':tok}))
            cl.send(json.dumps(room.lobby()))
            print(f"  Room {code} created by {name}")

        elif t=='join_room':
            code=msg.get('code','').upper().strip()
            name=msg.get('name','Player')[:20]
            token=msg.get('token','')
            if code not in rooms:
                cl.send(json.dumps({'type':'error','msg':'Room not found! Check code.'}));return
            room=rooms[code]
            # Reconnect with token?
            if token and token in room.tokens:
                old_slot=room.tokens[token]
                room.players[old_slot]=cl.id
                cl.room=code;cl.slot=old_slot;cl.token=token
                room.names[old_slot]=name
                cl.send(json.dumps({'type':'joined','slot':old_slot,'code':code,'name':name,'token':token,'reconnected':True}))
                if room.state:
                    gs=room.gstate(old_slot)
                    if gs:cl.send(json.dumps(gs))
                else:
                    cl.send(json.dumps(room.lobby()))
                room.bcast({'type':'player_rejoined','slot':old_slot,'name':name},exc=cl.id)
                print(f"  {name} reconnected to {code} as P{old_slot}")
                return
            if room.state:
                cl.send(json.dumps({'type':'error','msg':'Game already started!'}));return
            if room.full():
                cl.send(json.dumps({'type':'error','msg':'Room is full (4 players)!'}));return
            slot=room.free_slot()
            tok=''.join(random.choices(string.ascii_letters+string.digits,k=24))
            room.players[slot]=cl.id;room.names[slot]=name
            room.tokens[tok]=slot
            cl.room=code;cl.slot=slot;cl.token=tok
            cl.send(json.dumps({'type':'joined','slot':slot,'code':code,'name':name,'token':tok}))
            room.bcast(room.lobby())
            # Send chat history
            if room.chat:cl.send(json.dumps({'type':'chat_history','msgs':room.chat[-50:]}))
            print(f"  {name} joined {code} as P{slot}")

        elif t=='start_game':
            if not cl.room:return
            room=rooms.get(cl.room)
            if not room or cl.id!=room.host_id:return
            if room.pcount()<1:
                cl.send(json.dumps({'type':'error','msg':'Need at least 1 player!'}));return
            room.start()
            room.bcast({'type':'game_started',
                'names':{str(s):room.names.get(s,'P'+str(s))for s in range(1,5)}})
            room.bcast_gs()
            _ai(room);print(f"  Game started in {room.code}")

        elif t=='announce':
            if not cl.room or not cl.slot:return
            room=rooms.get(cl.room)
            if room and room.state:
                room.do_announce(cl.slot,msg.get('yes',False))
                _ai_ann(room)

        elif t=='play_card':
            if not cl.room or not cl.slot:return
            room=rooms.get(cl.room)
            if room and room.state:room.do_play(cl.slot,msg.get('card',''))

        elif t=='next_round':
            if not cl.room:return
            room=rooms.get(cl.room)
            if room and room.state and cl.id==room.host_id:
                room.next_round();_ai(room)

        elif t=='chat':
            if not cl.room:return
            room=rooms.get(cl.room)
            if room:
                txt=str(msg.get('text','')).strip()[:300]
                if txt:room.add_chat(cl.slot,txt)

        elif t=='ping':
            cl.send(json.dumps({'type':'pong'}))

        elif t=='get_history':
            if not cl.room:return
            room=rooms.get(cl.room)
            if room:cl.send(json.dumps({'type':'history','history':room.history,
                                        'names':{str(s):room.names.get(s,'P'+str(s))for s in range(1,5)}}))

# ── AI ────────────────────────────────────────────────────────
def _ai(room):
    st=room.state
    if not st or st['phase']!='play':return
    cur=st['cur']
    if room.ws_of(cur)is None:
        threading.Timer(0.9,_ai_play,args=(room,cur)).start()

def _ai_ann(room):
    st=room.state
    if not st or st['phase']!='announce':return
    for s in range(1,5):
        if room.ws_of(s)is None and s not in st['ann_done']:
            hand=st['hands'].get(s,[]);pen=sum(1 for c in hand if cpts(c)>0)
            yes=pen==0 and random.random()>.3
            threading.Timer(0.4*s,lambda sl=s,y=yes:_ai_do_ann(room,sl,y)).start()

def _ai_do_ann(room,slot,yes):
    with lock:
        if room.state and room.state['phase']=='announce'and slot not in room.state['ann_done']:
            room.do_announce(slot,yes)

def _ai_play(room,slot):
    with lock:
        st=room.state
        if not st or st['cur']!=slot or st['resolving']:return
        hand=st['hands'].get(slot,[]);led=st['led']
        idxs=playable(hand,led);avail=[hand[i]for i in idxs]
        if not avail:return
        if not led:
            safe=[c for c in avail if c['s']in('D','C')]
            ch=random.choice(safe)if safe else sorted(avail,key=lambda c:RV[c['r']])[0]
        else:
            sc=[c for c in avail if c['s']==led]
            if sc:ch=sorted(sc,key=lambda c:RV[c['r']])[0]
            else:
                pen=[c for c in avail if cpts(c)>0]
                ch=sorted(pen,key=lambda c:-cpts(c))[0]if pen else sorted(avail,key=lambda c:-RV[c['r']])[0]
        room.do_play(slot,ch['r']+ch['s'])
        threading.Timer(0.3,lambda:_ai(room)).start()

# ── Disconnect ────────────────────────────────────────────────
def on_disc(cl):
    with lock:
        clients.pop(cl.id,None)
        if cl.room and cl.room in rooms:
            room=rooms[cl.room];slot=cl.slot
            if slot and slot in room.players:
                nm=room.names.get(slot,'Player')
                if not room.state:
                    # Pre-game: remove them
                    room.players.pop(slot,None)
                    if cl.token:room.tokens.pop(cl.token,None)
                    room.bcast(room.lobby())
                else:
                    # In-game: mark disconnected (keep slot, AI takes over)
                    room.players[slot]=None
                    room.bcast({'type':'player_disconnected','slot':slot,'name':nm})
                    _ai(room)  # AI takes over if it's their turn

# ── WS Connection ─────────────────────────────────────────────
def handle_ws(conn,addr):
    cl=None
    try:
        req=b''
        while b'\r\n\r\n' not in req:
            c=conn.recv(1024)
            if not c:return
            req+=c
        key=None
        for line in req.decode('utf-8',errors='replace').split('\r\n'):
            if'Sec-WebSocket-Key'in line:key=line.split(':')[1].strip()
        if not key:conn.close();return
        wshs(conn,key)
        cl=Client(conn,addr)
        with lock:clients[cl.id]=cl
        print(f"  Client {cl.id} from {addr[0]}")
        while True:
            raw=wsrecv(conn)
            if raw is None:break
            if raw:handle_msg(cl,raw)
    except:pass
    finally:
        if cl:on_disc(cl)
        try:conn.close()
        except:pass

# ── HTTP ──────────────────────────────────────────────────────
def get_html():
    for name in ['hearts_final.html','hearts_v2.html','hearts_multiplayer.html']:
        p=os.path.join(os.path.dirname(os.path.abspath(__file__)),name)
        if os.path.exists(p):
            with open(p,'r',encoding='utf-8')as f:return f.read()
    return"<h1>Place hearts_final.html next to server_final.py</h1>"

def handle_connection(conn,addr):
    """Smart handler: WebSocket upgrade OR HTTP — both on same port."""
    try:
        req=b''
        while b'\r\n\r\n' not in req:
            chunk=conn.recv(4096)
            if not chunk:conn.close();return
            req+=chunk
        req_str=req.decode('utf-8',errors='replace')
        if'Upgrade: websocket'in req_str or'upgrade: websocket'in req_str.lower():
            key=None
            for line in req_str.split('\r\n'):
                if'Sec-WebSocket-Key'in line:key=line.split(':')[1].strip()
            if not key:conn.close();return
            wshs(conn,key)
            cl=Client(conn,addr)
            with lock:clients[cl.id]=cl
            print(f"  WS {cl.id} from {addr[0]}")
            while True:
                raw=wsrecv(conn)
                if raw is None:break
                if raw:handle_msg(cl,raw)
            on_disc(cl)
        else:
            path=''
            fl=req_str.split('\r\n')[0]
            if' 'in fl:path=urllib.parse.urlparse(fl.split(' ')[1]).path
            if path in('/','','/game','/index.html'):
                html=get_html().encode('utf-8')
                resp=("HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
                      f"Content-Length: {len(html)}\r\nConnection: close\r\n\r\n")
                conn.sendall(resp.encode()+html)
            else:
                conn.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
    except:pass
    finally:
        try:conn.close()
        except:pass

def get_ip():
    try:
        s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
        s.connect(("8.8.8.8",80));ip=s.getsockname()[0];s.close();return ip
    except:return"127.0.0.1"

def main():
    srv=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
    srv.bind((HOST,PORT));srv.listen(50)
    ip=get_ip()
    print()
    print("╔══════════════════════════════════════════════════════════╗")
    print("║   ♥ ♦  HEARTS & SPADES — MULTIPLAYER  ♣ ♠              ║")
    print("║   ONE port for HTTP + WebSocket. Works with ngrok!       ║")
    print("╠══════════════════════════════════════════════════════════╣")
    print(f"║  Same WiFi:  http://{ip}:{PORT}".ljust(57)+"║")
    print("╠══════════════════════════════════════════════════════════╣")
    print("║  INTERNET (any network, any country):                    ║")
    print("║  1. Open a NEW terminal                                  ║")
    print(f"║  2. Run:  ngrok http {PORT}                               ║")
    print("║  3. Share the https://xxxx.ngrok-free.app link           ║")
    print("╚══════════════════════════════════════════════════════════╝")
    print(f"\n  Running on port {PORT}... Ctrl+C to stop.\n")
    def accept_loop():
        while True:
            try:
                conn,addr=srv.accept()
                threading.Thread(target=handle_connection,args=(conn,addr),daemon=True).start()
            except:break
    threading.Thread(target=accept_loop,daemon=True).start()
    try:
        while True:time.sleep(1)
    except KeyboardInterrupt:
        print("\n  Server stopped. Thanks for playing! ♥")
        srv.close()

if __name__=='__main__':main()
