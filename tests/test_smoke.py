def test_приложение_импортируется(app_ctx):
    assert app_ctx.application.name
