import os, json, hashlib
from flask import Flask, request, abort
import firebase_admin
from firebase_admin import credentials, auth, firestore, messaging
firebase_admin.initialize_app(credentials.Certificate(json.loads(os.environ['FIREBASE_SERVICE_ACCOUNT'])))
db = firestore.client()
app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 16384

def uid():
    try: return auth.verify_id_token(request.headers.get('Authorization','').removeprefix('Bearer '), check_revoked=True)['uid']
    except Exception: abort(401)

def token_ref(token):
    if not isinstance(token,str) or not 20 <= len(token) <= 4096: abort(400)
    return db.collection('device_tokens').document(hashlib.sha256(token.encode()).hexdigest())

@app.get('/')
def health(): return {'ok': True}

@app.post('/register')
def register():
    u = uid(); token = request.json.get('token'); token_ref(token).set({'uid':u,'token':token}); return {'ok':True}

@app.post('/unregister')
def unregister():
    u = uid(); ref = token_ref(request.json.get('token'))
    @firestore.transactional
    def remove(tx):
        doc = ref.get(transaction=tx)
        if doc.exists and doc.to_dict()['uid'] == u: tx.delete(ref)
    remove(db.transaction()); return {'ok':True}

@app.post('/event')
def event():
    u = uid(); data = request.json; bid = data.get('bookingId'); kind = data.get('type')
    if not isinstance(bid,str) or '/' in bid or not bid or len(bid)>200: abort(400)
    doc = db.collection('bookings').document(bid).get()
    if not doc.exists: abort(404)
    b = doc.to_dict(); owner = b['ownerUid']; status = b['status']
    if kind == 'created' and owner == u and status == 'รออนุมัติ':
        recipients = {owner: ('ส่งคำขอสำเร็จ','คำขอจองของคุณกำลังรออนุมัติ')}
        for a in db.collection('users').where('role','==','admin').stream(): recipients[a.id] = ('คำขอจองใหม่','มีคำขอจองใหม่ กรุณาตรวจสอบใน BookMe')
        version = 'created'
    elif kind == 'updated' and (db.collection('users').document(u).get().to_dict() or {}).get('role') == 'admin':
        recipients = {owner: ('อัปเดตการจอง',f'สถานะการจอง: {status} กรุณาตรวจสอบรายละเอียดใน BookMe')}
        version = hashlib.sha256(json.dumps([status,b.get('details'),b.get('assignedVehicleId'),b.get('assignedDriverId')],ensure_ascii=False).encode()).hexdigest()
    else: abort(403)
    for recipient,(title,body) in recipients.items():
        key = hashlib.sha256(f'{bid}:{version}'.encode()).hexdigest()
        ref = db.collection('users').document(recipient).collection('notifications').document(key)
        try: ref.create({'title':title,'body':body,'bookingId':bid,'read':False,'createdAt':firestore.SERVER_TIMESTAMP})
        except Exception:
            if ref.get().exists: continue
            raise
        for device in db.collection('device_tokens').where('uid','==',recipient).stream():
            try: messaging.send(messaging.Message(token=device.to_dict()['token'], notification=messaging.Notification(title=title,body=body), android=messaging.AndroidConfig(priority='high')))
            except messaging.UnregisteredError: device.reference.delete()
            except Exception: app.logger.exception('FCM send failed')
    return {'ok':True}
