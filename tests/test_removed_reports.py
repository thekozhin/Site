import pytest


@pytest.fixture
def reports_client(app_ctx):
    mod=app_ctx
    user=mod.User(username='reports-audit',password='unused',company='Сетранс',allowed_parks=['14 парк'])
    mod.db.session.add(user)
    mod.db.session.commit()
    client=mod.application.test_client()
    with client.session_transaction() as sess:
        sess['_user_id']=str(user.id)
        sess['_fresh']=True
        sess['selected_park']='14 парк'
        sess['available_parks']=['14 парк']
    return mod,client


def test_removed_reports_are_not_accessible_or_linked(reports_client):
    mod,client=reports_client
    for endpoint in ['report_7','report_14','report_17','report_19']:
        assert endpoint not in mod.application.view_functions
        assert client.get('/'+endpoint).status_code==404
    response=client.get('/reports')
    assert response.status_code==200
    assert '/itr_7' in response.text
    for endpoint in ['report_7','report_14','report_17','report_19']:
        assert '/'+endpoint not in response.text


def test_snapshot_tables_and_ktg_remain_available(reports_client):
    mod,client=reports_client
    for model in [mod.Report7Table,mod.Report830Table,mod.Report17Table,mod.Report19Table]:
        assert mod.db.session.query(model).count()==0
    assert client.get('/reports_ktg').status_code==200
    assert client.get('/dashboard').status_code==200
