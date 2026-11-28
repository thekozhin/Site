import base64
import json
import time
from unittest.mock import Mock
from incontrol import IncontrolClient
from incontrol import capture_session, restore_session, ensure_session


def token(exp):
    return 'header.'+base64.urlsafe_b64encode(json.dumps({'exp':exp}).encode()).decode().rstrip('=')+'.signature'


def test_live_token_is_not_refreshed_for_each_step():
    client=IncontrolClient('user','password',interactive=True)
    client._apply_tokens({'access':token(time.time()+300),'refresh':'refresh'})
    client._renew=Mock();client.authenticate=Mock()
    ensure_session(client)
    client._renew.assert_not_called();client.authenticate.assert_not_called()
    client.session.close()


def test_expired_token_uses_refresh_not_password_login():
    client=IncontrolClient('user','password',interactive=True)
    client._apply_tokens({'access':token(time.time()-1),'refresh':'refresh'})
    client._renew=Mock();client.authenticate=Mock()
    ensure_session(client)
    client._renew.assert_called_once();client.authenticate.assert_not_called()
    client.session.close()


def test_signature_session_cookie_survives_between_clients_and_expires():
    first=IncontrolClient('user','password',interactive=True)
    first._apply_tokens({'access':token(time.time()+300),'refresh':'refresh'})
    first.session.cookies.set('signature_session','server-capability',domain='skk.mttech.moscow',path='/',secure=True,expires=int(time.time()+43200),rest={'HttpOnly':True})
    first.session.cookies.set('expired','old',domain='skk.mttech.moscow',expires=int(time.time()-1))
    first.session.cookies.set('untrusted','value',domain='example.test')
    saved=capture_session(first)
    assert [x['name'] for x in saved['cookies']]==['signature_session']
    second=IncontrolClient('user','password',interactive=True)
    restore_session(second,saved)
    assert second.session.cookies.get('signature_session')=='server-capability'
    assert second.session.headers['Authorization']==first.session.headers['Authorization']
    for c in [first,second]:c.session.close()


def test_auth_audit_records_refresh_and_fallback_without_credentials(caplog):
    import logging
    from unittest.mock import Mock
    client=IncontrolClient('private-login','private-password',interactive=True)
    client._apply_tokens({'access':'private-access','refresh':'private-refresh'})
    client.auth_context='run:test'
    client.session.post=Mock(return_value=Mock(ok=True,status_code=200,json=lambda:{'access':'new-private-access'}))
    with caplog.at_level(logging.INFO,logger='app'):
        client._renew()
    assert 'event=refresh' in caplog.text and 'run:test' in caplog.text
    assert 'private-' not in caplog.text
    caplog.clear();client.authenticate=Mock()
    client.session.post.return_value=Mock(ok=False,status_code=401)
    with caplog.at_level(logging.INFO,logger='app'):
        client._renew()
    assert 'event=refresh_rejected' in caplog.text
    client.authenticate.assert_called_once()
    client.session.close()
