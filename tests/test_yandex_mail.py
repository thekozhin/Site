import errno
import imaplib
import smtplib
from contextlib import nullcontext
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from io import BytesIO
from unittest.mock import Mock

import pytest

import yandex_mail
from openpyxl import Workbook

from completion_checks import utcnow
from test_completion_checks import complete, completion, run_check, saved_task
from test_incontrol_rejection_jobs import rejection_page
from yandex_mail import (MOSCOW, MAIL_TASK_AUTHOR, ImapConnectionError, MailError, OriginalMail,
                         PermanentDeliveryError, UncertainDelivery, YandexMailbox)


ADDRESS = 'setrans-test@yandex.ru'
PARK = '14 парк'
FOLDER = '14АП NEW'
ORIGINAL_ID = '<repair-42@example.org>'
WORK = 'Отрегулировали рулевую колонку\nЗаменили уплотнение'


def test_sync_logs_imap_stage_and_code_without_raw_response(app_ctx, monkeypatch, caplog):
    monkeypatch.setenv('YANDEX_MAIL_ADDRESS', ADDRESS)
    monkeypatch.setenv('YANDEX_MAIL_APP_PASSWORD', 'mock-app-password')
    monkeypatch.setenv('YANDEX_MAIL_AUTH', 'password')
    connection = Mock()
    connection.login.side_effect = imaplib.IMAP4.error('[AUTHENTICATIONFAILED] mock-app-password')
    monkeypatch.setattr(imaplib, 'IMAP4_SSL', Mock(return_value=connection))

    result = app_ctx.yandex_requests.run()
    preview = app_ctx.application.test_cli_runner().invoke(args=['yandex-mail-sync'])

    assert result['status'] == 'error' and preview.exit_code == 1
    assert 'режим=по расписанию' in caplog.text and 'режим=просмотр' in caplog.text
    assert 'этап=IMAP' in caplog.text and 'AUTHENTICATIONFAILED' in caplog.text
    assert 'mock-app-password' not in caplog.text + preview.output
    assert connection.logout.call_count == 2


def test_invalid_import_cutoff_does_not_block_reply_queue(app_ctx, monkeypatch):
    requests = app_ctx.yandex_requests
    monkeypatch.setenv('YANDEX_MAIL_ADDRESS', ADDRESS)
    monkeypatch.setenv('YANDEX_MAIL_IMPORT_FROM', 'некорректная дата')
    mail = Mock(spec=YandexMailbox)
    lookup, deliver, load_requests = Mock(), Mock(), Mock()
    monkeypatch.setattr(requests, '_mailbox', lambda *_: nullcontext(mail))
    monkeypatch.setattr(requests, 'resolve_reply_lookups', lookup)
    monkeypatch.setattr(requests, 'deliver_replies', deliver)
    monkeypatch.setattr(requests, 'import_requests', load_requests)

    assert requests.run()['status'] == 'error'
    lookup.assert_called_once_with(mail, ADDRESS, requests.folders())
    deliver.assert_called_once_with(mail, ADDRESS)
    load_requests.assert_not_called()


def test_smtp_logs_authorization_code_once_per_sync(monkeypatch, caplog):
    connection = Mock()
    connection.login.side_effect = smtplib.SMTPAuthenticationError(535, b'private-provider-message')
    monkeypatch.setattr(smtplib, 'SMTP_SSL', Mock(return_value=connection))
    mailbox = YandexMailbox(ADDRESS, 'mock-app-password')

    for _ in range(2):
        with pytest.raises(MailError):
            mailbox.send_reply(EmailMessage())

    assert caplog.text.count('сбой авторизации (535)') == 1
    assert 'private-provider-message' not in caplog.text
    connection.send_message.assert_not_called()


@pytest.mark.parametrize(('code', 'expected'), [(451, MailError), (550, PermanentDeliveryError)])
def test_smtp_recipient_rejection_distinguishes_temporary_and_permanent_errors(code, expected):
    mailbox = YandexMailbox(ADDRESS, 'mock-app-password')
    mailbox.smtp = Mock()
    mailbox.smtp.send_message.side_effect = smtplib.SMTPRecipientsRefused({
        'repair@example.org': (code, b'private-provider-message'),
    })
    with pytest.raises(expected, match=str(code)) as caught:
        mailbox.send_reply(EmailMessage())
    assert type(caught.value) is expected
    assert 'private-provider-message' not in str(caught.value)


def test_imap_disconnect_records_command_without_provider_data():
    mailbox = YandexMailbox(ADDRESS, 'mock-secret')
    mailbox.imap = Mock()
    mailbox.imap.uid.side_effect = imaplib.IMAP4.abort('private-provider-message')
    with pytest.raises(ImapConnectionError, match='IMAP UID SEARCH') as error:
        mailbox._search('UNANSWERED')
    assert 'private-provider-message' not in str(error.value)


@pytest.mark.parametrize(('mode', 'expected'), [
    ('bye', 'сервер прислал BYE'),
    ('eof', 'EOF от соединения'),
    ('reset', f'ConnectionResetError, errno={errno.ECONNRESET}'),
])
def test_fetch_failure_includes_batch_timing_and_safe_cause(mode, expected, monkeypatch):
    mailbox = YandexMailbox(ADDRESS, 'mock-secret', 'Sent')
    mailbox.imap = Mock(untagged_responses={'BYE': [b'private-provider-message']} if mode == 'bye' else {})
    mailbox.imap.select.return_value = 'OK', [b'65']
    ticks = Mock(side_effect=[1, 20, 26.5])
    monkeypatch.setattr(yandex_mail, 'monotonic', ticks)
    uids = [str(uid).encode() for uid in range(1, 66)]

    def reply(command, _, *args):
        if command == 'SEARCH':
            return 'OK', [b' '.join(uids)]
        if mailbox.imap.uid.call_count == 2:
            return 'OK', [None]
        try:
            if mode == 'reset':
                try:
                    raise ConnectionResetError(errno.ECONNRESET, 'private-provider-message')
                except OSError:
                    raise imaplib.IMAP4.abort('private-provider-message')
            raise imaplib.IMAP4.abort('socket error: EOF' if mode == 'eof' else 'private-provider-message')
        except imaplib.IMAP4.abort as exc:
            raise imaplib.IMAP4.abort(f'command: UID => {exc}')
    mailbox.imap.uid.side_effect = reply

    with pytest.raises(ImapConnectionError) as error:
        mailbox.reply_parents_since(date(2026, 11, 25))

    details = str(error.value)
    assert 'IMAP UID FETCH' in details and expected in details
    assert 'FETCH: заголовки отправленных ответов, писем в пакете=1' in details
    assert 'обработано до сбоя=64/65, команда=6.50 с' in details
    assert 'private-provider-message' not in details


def test_sent_replies_are_checked_in_one_date_query_using_exact_headers():
    mailbox = YandexMailbox(ADDRESS, 'mock-secret', 'Sent')
    mailbox.imap = Mock()
    mailbox.imap.select.return_value = 'OK', [b'3']
    mailbox.imap.uid.side_effect = [
        ('OK', [b'1 2 3']),
        ('OK', [
            (b'1 (UID 1 FLAGS (\\Seen)', b'In-Reply-To: <first@example.org>\r\n\r\n'),
            (b'2 (UID 2 FLAGS ()', b'References: <ancestor@example.org> <second@example.org>\r\n\r\n'),
            (b'3 (UID 3 FLAGS (\\Deleted)', b'In-Reply-To: <deleted@example.org>\r\n\r\n'),
        ]),
    ]

    assert mailbox.reply_parents_since(date(2026, 11, 23)) == {'<first@example.org>', '<second@example.org>'}
    assert mailbox.has_reply('<first@example.org>', datetime(2026, 11, 23, 9, tzinfo=MOSCOW))
    assert not mailbox.has_reply('<ancestor@example.org>', datetime(2026, 11, 23, 9, tzinfo=MOSCOW))
    assert mailbox.imap.uid.call_args_list[0].args == ('SEARCH', None, 'SINCE', '22-Nov-2026')
    assert mailbox.imap.uid.call_count == 2


def test_scheduled_import_reopens_imap_after_disconnect(app_ctx, monkeypatch):
    requests = app_ctx.yandex_requests
    monkeypatch.setenv('YANDEX_MAIL_ADDRESS', ADDRESS)
    first, second = Mock(spec=YandexMailbox), Mock(spec=YandexMailbox)
    connection = Mock(side_effect=[nullcontext(first), nullcontext(second)])
    importer = Mock(side_effect=[ImapConnectionError('IMAP UID SEARCH'), {'created': 1}])
    monkeypatch.setattr(requests, '_mailbox', connection)
    monkeypatch.setattr(requests, 'import_requests', importer)

    assert requests.run(import_only=True) == {'created': 1}
    assert [call.args[0] for call in importer.call_args_list] == [first, second]


def test_scheduled_replies_retry_imap_and_process_linked_replies_first(app_ctx, monkeypatch):
    requests = app_ctx.yandex_requests
    monkeypatch.setenv('YANDEX_MAIL_ADDRESS', ADDRESS)
    first, second = Mock(spec=YandexMailbox), Mock(spec=YandexMailbox)
    monkeypatch.setattr(requests, '_mailbox', Mock(side_effect=[nullcontext(first), nullcontext(second)]))
    monkeypatch.setattr(requests, '_has_reply_work', lambda: True)
    steps = Mock()
    steps.lookup.side_effect = [ImapConnectionError('IMAP EXAMINE'), None]
    monkeypatch.setattr(requests, 'deliver_replies', steps.deliver)
    monkeypatch.setattr(requests, 'resolve_reply_lookups', steps.lookup)

    assert requests.run(replies_only=True) is None
    assert [call[0] for call in steps.mock_calls] == ['deliver', 'lookup', 'deliver', 'lookup']


def test_scheduled_reply_timeout_defers_only_affected_message(app_ctx, monkeypatch):
    mod = app_ctx
    requests = mod.yandex_requests
    monkeypatch.setenv('YANDEX_MAIL_FOLDERS', '{"14 парк":"INBOX"}')
    boards = ('164076', '164077')
    mod.db.session.add_all(mod.Car(bort=board, location=PARK) for board in boards)
    mod.db.session.commit()
    received = datetime.now(MOSCOW)
    other_id = '<other-board@example.org>'
    messages = fake_mail(received, {
        ORIGINAL_ID: mail_message(ORIGINAL_ID, 'УА160222071', boards[0]),
        other_id: mail_message(other_id, 'УА160222072', boards[1]),
    })
    messages.unanswered.return_value = [[OriginalMail(b'1', ORIGINAL_ID, received, boards[0]),
                                        OriginalMail(b'2', other_id, received, boards[1])]]
    assert requests.import_requests(messages, ADDRESS, {PARK: 'INBOX'}, None, apply=True)['created'] == 2
    for task in mod.Task.query.order_by(mod.Task.id):
        task.status = 'completed'
        task.completed_work = WORK
        requests.queue_reply(task)
    mod.db.session.commit()

    attempted = []
    def deliver(mail, item):
        attempted.append(item.board_number)
        if item.board_number == boards[0]:
            raise ImapConnectionError('IMAP UID SEARCH (TimeoutError)')
    monkeypatch.setattr(requests, '_deliver', deliver)

    with pytest.raises(ImapConnectionError):
        requests.deliver_replies(messages, ADDRESS)
    assert requests.deliver_replies(messages, ADDRESS) == 1
    assert attempted == list(boards)
    link = requests.model.query.filter_by(board_number=boards[0]).one()
    assert link.reply_state == 'pending'
    assert link.reply_next_at > utcnow() and link.reply_retry_count == 0


def test_immediate_reply_reopens_imap_before_retry(app_ctx, monkeypatch):
    requests = app_ctx.yandex_requests
    monkeypatch.setenv('YANDEX_MAIL_ADDRESS', ADDRESS)
    first, second = Mock(spec=YandexMailbox), Mock(spec=YandexMailbox)
    connection = Mock(side_effect=[nullcontext(first), nullcontext(second)])
    lookup = Mock(side_effect=[ImapConnectionError('IMAP UID SEARCH'), None])
    deliver = Mock()
    monkeypatch.setattr(requests, '_mailbox', connection)
    monkeypatch.setattr(requests, 'resolve_reply_lookups', lookup)
    monkeypatch.setattr(requests, 'deliver_replies', deliver)

    assert requests.reply_slots.acquire(blocking=False)
    requests._run_reply(123, '164076')
    assert connection.call_count == 2
    deliver.assert_called_once_with(second, ADDRESS, task_id=123)


def mail_message(original_id, ticket_number, board='164076', sheet_board=None, *,
                 defects=('Течь рулевой рейки', 'Стук'), subject=None, sender='repair@example.org',
                 service_type=None):
    workbook = Workbook()
    sheet = workbook.active
    sheet['Q1'], sheet['AH1'] = 'ЗАЯВКА НА РЕМОНТ №', ticket_number
    sheet['D6'], sheet['J6'] = 'Автобус:', f'{sheet_board or board} НЕФАЗ 5299'
    if service_type:
        sheet['P13'], sheet['P15'], sheet['G21'] = 'Вид ремонта', service_type, 'ЗАКАЗЧИК'
    else:
        sheet['H20'] = 'Описание неисправности'
        for row, defect in enumerate(defects):
            sheet[f'H{22 + row * 2}'] = defect
        sheet[f'G{22 + len(defects) * 2}'], sheet['H45'] = 'ЗАКАЗЧИК', 'Текст из акта'
    attachment = BytesIO()
    workbook.save(attachment)

    message = EmailMessage()
    message['From'] = f'Приёмка <{sender}>'
    message['To'] = ADDRESS
    message['Subject'] = subject or board
    message['Message-ID'] = original_id
    message.set_content('Заявка во вложении')
    message.add_attachment(attachment.getvalue(), maintype='application',
                           subtype='vnd.openxmlformats-officedocument.spreadsheetml.sheet', filename=f'{board}.xlsx')
    return message.as_bytes()


def fake_mail(received_at, messages):
    mailbox = Mock(spec=YandexMailbox)
    originals = [OriginalMail(str(i).encode(), key, received_at) for i, key in enumerate(messages, 1)]
    mailbox.unanswered.return_value = [originals]
    mailbox.download.side_effect = lambda folder, original: messages[original.message_id]
    mailbox.has_reply.return_value = False
    mailbox.reply_parents_since.return_value = set()
    mailbox.has_sent_copy.return_value = False
    mailbox.original_answered.return_value = False
    return mailbox


@pytest.mark.parametrize(('start_date', 'expected_uids', 'date_terms'), [
    (date(2026, 11, 20), [b'10', b'14'], ('SINCE', '19-Nov-2026')),
    (datetime(2026, 11, 20, 0, 0, 1), [b'14'], ('SINCE', '19-Nov-2026')),
    (None, [b'10', b'11', b'13', b'14'], ()),
])
def test_imap_filters_optional_moscow_date_and_answered_flags(start_date, expected_uids, date_terms):
    mailbox = YandexMailbox(ADDRESS, 'mock-secret')
    mailbox.imap = Mock()
    mailbox.imap.select.return_value = ('OK', [b'3'])

    def meta(uid, flags, stamp):
        frame = f'1 (UID {uid} FLAGS ({flags}) RFC822.SIZE 1234 INTERNALDATE "{stamp}"'.encode()
        return frame, f'Message-ID: <mail-{uid}@example.org>\r\n\r\n'.encode()

    mailbox.imap.uid.side_effect = [
        ('OK', [b'10 11 12 13 14']),
        ('OK', [meta(10, '', '19-Nov-2026 21:00:00 +0000'),
                meta(11, '', '20-Nov-2026 00:00:00 +0500'),
                meta(12, r'\Answered', '20-Nov-2026 12:00:00 +0300'),
                meta(13, '', '16-Nov-2026 00:00:00 +0300'),
                meta(14, '', '19-Nov-2026 21:00:01 +0000')]),
    ]
    batch, = mailbox.unanswered(FOLDER, start_date)
    assert [item.uid for item in batch] == expected_uids
    mailbox.imap.select.assert_called_once_with('"14&BBAEHw- NEW"', readonly=True)
    assert mailbox.imap.uid.call_args_list[0].args == ('SEARCH', None, 'UNANSWERED', 'UNDELETED', *date_terms)


@pytest.mark.parametrize(('configured', 'expected'), [
    ('', None),
    ('2026-11-21', datetime(2026, 11, 21)),
    ('2026-11-21T20:36:03', datetime(2026, 11, 21, 20, 36, 3)),
])
def test_inbox_import_settings_accept_date_and_time(app_ctx, monkeypatch, configured, expected):
    monkeypatch.setenv('YANDEX_MAIL_ADDRESS', ADDRESS)
    monkeypatch.delenv('YANDEX_MAIL_FOLDERS', raising=False)
    monkeypatch.setenv('YANDEX_MAIL_IMPORT_FROM', configured)

    _, _, folders, start = app_ctx.yandex_requests.settings()

    assert folders == {PARK: 'INBOX'} and start == expected


def test_inbox_search_keeps_only_subjects_with_one_board():
    mailbox = YandexMailbox(ADDRESS, 'mock-secret')
    mailbox.imap = Mock()
    mailbox.imap.select.return_value = 'OK', [b'3']

    def meta(uid, subject):
        frame = f'1 (UID {uid} FLAGS () RFC822.SIZE 1234 INTERNALDATE "20-Nov-2026 11:00:00 +0300"'.encode()
        return frame, f'Message-ID: <mail-{uid}@example.org>\r\nSubject: {subject}\r\n\r\n'.encode()

    mailbox.imap.uid.side_effect = [
        ('OK', [b'10 11 12']),
        ('OK', [meta(10, '164076'), meta(11, 'Other correspondence'), meta(12, '164076 162758')]),
    ]
    batch, = mailbox.unanswered('INBOX', date(2026, 11, 20))

    assert [item.board_number for item in batch] == ['164076']
    mailbox.imap.select.assert_called_once_with('"INBOX"', readonly=True)
    assert 'BODY.PEEK[HEADER.FIELDS (MESSAGE-ID SUBJECT)]' in mailbox.imap.uid.call_args.args[-1]


def test_original_lookup_falls_back_to_dates_and_compares_message_id():
    mail = YandexMailbox(ADDRESS, 'mock-secret')
    mail.imap = Mock()
    mail.imap.select.return_value = 'OK', [b'3']
    mail.imap.uid.side_effect = [
        ('NO', [b'[UNAVAILABLE] UID SEARCH Backend error.']),
        ('OK', [b'1 2 3']),
        ('OK', [
            (b'1 (UID 1 FLAGS (\\Answered)', b'Message-ID: <other@example.org>\r\n\r\n'),
            (b'2 (UID 2 FLAGS (\\Deleted)', f'Message-ID: {ORIGINAL_ID}\r\n\r\n'.encode()),
            (b'3 (UID 3 FLAGS ()', f'Message-ID: {ORIGINAL_ID}\r\n\r\n'.encode()),
        ]),
    ]
    received_at = datetime(2026, 11, 16, 10, 19)

    assert mail.original_answered(FOLDER, ORIGINAL_ID, received_at) is False
    assert mail.imap.uid.call_args_list[1].args == ('SEARCH', None, 'UNDELETED', 'SINCE', '15-Nov-2026',
                                                   'BEFORE', '18-Nov-2026')
    assert mail.imap.uid.call_args.args == ('FETCH', b'1,2,3', '(UID FLAGS BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])')


@pytest.mark.parametrize('flags', [b'', b'\\Answered'])
def test_mark_answered_adds_important_flag_even_if_already_answered(flags):
    mail = YandexMailbox(ADDRESS, 'mock-secret')
    mail.imap = Mock()
    mail.imap.select.return_value = 'OK', [b'1']
    mail.imap.uid.side_effect = [
        ('OK', [b'37']),
        ('OK', [(b'1 (UID 37 FLAGS (' + flags + b')', f'Message-ID: {ORIGINAL_ID}\r\n\r\n'.encode())]),
        ('OK', [None]),
    ]

    mail.mark_answered(FOLDER, ORIGINAL_ID, datetime(2026, 11, 16, 10, 19))

    mail.imap.select.assert_called_once_with('"14&BBAEHw- NEW"', readonly=False)
    assert mail.imap.uid.call_args.args == ('STORE', b'37', '+FLAGS.SILENT', r'(\Answered \Flagged)')


def test_sent_copy_lookup_does_not_append_an_existing_copy_after_search_error():
    mail = YandexMailbox(ADDRESS, 'mock-secret', 'Sent')
    mail.imap = Mock()
    mail.imap.select.return_value = 'OK', [b'1']
    mail.imap.uid.side_effect = [
        ('NO', [b'[UNAVAILABLE] UID SEARCH Backend error.']),
        ('OK', [b'1']),
        ('OK', [(b'1 (UID 1 FLAGS ()', f'Message-ID: {ORIGINAL_ID}\r\n\r\n'.encode())]),
    ]
    reply = EmailMessage()
    reply['Message-ID'] = ORIGINAL_ID
    reply['Date'] = 'Sat, 21 Nov 2026 00:10:00 +0300'

    mail.save_sent_copy(reply)

    assert mail.imap.uid.call_args_list[1].args == ('SEARCH', None, 'UNDELETED', 'SINCE', '20-Nov-2026',
                                                   'BEFORE', '23-Nov-2026')
    mail.imap.append.assert_not_called()


def test_inbox_filters_boards_by_park_before_downloading(app_ctx, capsys):
    mod = app_ctx
    mod.db.session.add_all([
        mod.Car(bort='164076', location=PARK),
        mod.Car(bort='164054', location='1 парк'),
    ])
    mod.db.session.commit()
    other = '<other-park@example.org>'
    received = datetime(2026, 11, 20, 11, 20, tzinfo=MOSCOW)
    mail = fake_mail(received, {
        ORIGINAL_ID: mail_message(ORIGINAL_ID, 'УА160222071'),
        other: mail_message(other, 'УА160222072', board='164054'),
    })
    first = OriginalMail(b'1', ORIGINAL_ID, received, '164076')
    mail.unanswered.return_value = [[first, OriginalMail(b'2', other, received, '164054')]]

    preview = mod.yandex_requests.import_requests(mail, ADDRESS, {PARK: 'INBOX'}, received.date(), apply=False)
    assert preview['preview'] == preview['unknown_board'] == 1
    assert 'УА160222071' in capsys.readouterr().out
    assert mod.Task.query.count() == 0
    mail.download.assert_called_once_with('INBOX', first)

    result = mod.yandex_requests.import_requests(mail, ADDRESS, {PARK: 'INBOX'}, received.date(), apply=True)
    assert result['created'] == result['unknown_board'] == 1
    assert mod.yandex_requests.model.query.one().folder == 'INBOX'


def test_import_distinguishes_repair_numbers_and_skips_replies(app_ctx, caplog):
    mod = app_ctx
    mod.db.session.add(mod.Car(bort='164076', location=PARK))
    mod.db.session.commit()
    messages = {
        ORIGINAL_ID: mail_message(ORIGINAL_ID, 'УА160222071'),
        '<other@example.org>': mail_message('<other@example.org>', 'УА160222072'),
        '<duplicate@example.org>': mail_message('<duplicate@example.org>', 'УА160222071'),
        '<answered@example.org>': mail_message('<answered@example.org>', 'УА160222073'),
        '<wrong@example.org>': mail_message('<wrong@example.org>', 'УА160222074', sheet_board='164054'),
    }
    received = datetime(2026, 11, 20, 11, 20, tzinfo=MOSCOW)
    mail = fake_mail(received, messages)
    mail.reply_parents_since.return_value = {'<answered@example.org>'}
    args = mail, ADDRESS, {PARK: FOLDER}, received.date()
    first = mod.yandex_requests.import_requests(*args, apply=True)
    mail.reply_parents_since.assert_called_once_with(received.date())
    assert first == dict(created=1, linked=1, updated=0, duplicate=1, active_board=0, answered=1, invalid=1, unknown_board=0, preview=0)
    assert '14 парк 164076 добавлена заявка' in caplog.text
    caplog.clear()
    second = mod.yandex_requests.import_requests(*args, apply=True)
    assert second['created'] == 0 and mod.Task.query.count() == 1
    assert 'добавлена заявка' not in caplog.text
    assert {task.description for task in mod.Task.query} == {'Течь рулевой рейки\nСтук'}
    assert {task.tag for task in mod.Task.query} == {'waiting'}


@pytest.mark.parametrize('subject', ['162828 Новое', '162828 ДОПОЛНЕНИЕ', '162828'])
@pytest.mark.parametrize('latest_first', [False, True])
def test_new_mail_with_board_in_subject_replaces_description_and_gets_its_own_reply(app_ctx, subject, latest_first):
    mod = app_ctx
    requests = mod.yandex_requests
    mod.db.session.add(mod.Car(bort='162828', location=PARK))
    mod.db.session.commit()
    received = datetime.now(MOSCOW)
    new_id = '<supplement@example.org>'
    mail = fake_mail(received, {
        ORIGINAL_ID: mail_message(ORIGINAL_ID, 'УА160223600', '162828', defects=('Старое описание',)),
        new_id: mail_message(new_id, 'УА160223601', '162828', subject=subject,
                             defects=('Новое описание', 'Фара ближнего света')),
    })
    args = mail, ADDRESS, {PARK: 'INBOX'}, None
    latest = OriginalMail(b'1', new_id, received, '162828')
    older = OriginalMail(b'2', ORIGINAL_ID, received - timedelta(minutes=15), '162828')
    mail.unanswered.return_value = [[latest if latest_first else older]]
    assert requests.import_requests(*args, apply=True)['created'] == 1
    mail.unanswered.return_value = [[older, latest]]
    assert requests.import_requests(*args, apply=True)['linked'] == 1
    assert requests.import_requests(*args, apply=True)['duplicate'] == 2
    task = mod.Task.query.one()
    assert task.description == 'Новое описание\nФара ближнего света'
    task_id = task.id
    task.status = 'completed'
    task.completed_work = WORK
    assert len(requests.queue_reply(task)) == 2
    mod.db.session.commit()

    assert requests.deliver_replies(mail, ADDRESS, task_id=task_id) == 2
    assert {str(call.args[0]['In-Reply-To']) for call in mail.send_reply.call_args_list} == {ORIGINAL_ID, new_id}
    assert {item.reply_state for item in requests._links(task_id)} == {'sent'}
    assert requests.deliver_replies(mail, ADDRESS, task_id=task_id) == 0


def test_same_ticket_newer_workbook_replaces_description_once(app_ctx, caplog):
    mod = app_ctx
    requests = mod.yandex_requests
    board, ticket = '162841', 'УА160225248'
    mod.db.session.add(mod.Car(bort=board, location=PARK))
    mod.db.session.commit()
    received = datetime(2026, 11, 25, 7, 23, tzinfo=MOSCOW)
    wrong_sender_id = '<other-sender@example.org>'
    revision_id = '<revision@example.org>'
    stale_id = '<stale@example.org>'
    supplement_id = '<older-supplement@example.org>'
    closed_id = '<after-close@example.org>'
    updated = ('Фара ближнего света', 'Блокировка хода при открытых дверях', 'Ошибки на панели')
    mail = fake_mail(received, {
        ORIGINAL_ID: mail_message(ORIGINAL_ID, ticket, board, defects=('Фара ближнего света',)),
        revision_id: mail_message(revision_id, ticket, board, defects=updated),
        stale_id: mail_message(stale_id, ticket, board, defects=('Ошибки на панели',)),
        supplement_id: mail_message(supplement_id, 'УА160225249', board, defects=('Старый текст',)),
        closed_id: mail_message(closed_id, ticket, board, defects=('После закрытия',)),
        wrong_sender_id: mail_message(wrong_sender_id, ticket, board, defects=('Чужое письмо',),
                                     sender='another@example.org'),
    })

    def sync(uid, message_id, minutes):
        mail.unanswered.return_value = [[OriginalMail(str(uid).encode(), message_id,
                                                      received + timedelta(minutes=minutes), board)]]
        return requests.import_requests(mail, ADDRESS, {PARK: 'INBOX'}, None, apply=True)

    assert sync(1, ORIGINAL_ID, 0)['created'] == 1
    task_id = mod.Task.query.one().id
    assert sync(4, revision_id, 15)['updated'] == 1
    assert '14 парк 162841 изменено описание неисправностей' in caplog.text
    caplog.clear()
    mail.download.reset_mock()
    assert sync(4, revision_id, 15)['duplicate'] == 1
    mail.download.assert_not_called()
    assert sync(2, stale_id, 5)['duplicate'] == 1
    assert sync(3, supplement_id, 10)['linked'] == 1
    assert sync(5, wrong_sender_id, 20)['duplicate'] == 1
    assert 'изменено описание неисправностей' not in caplog.text

    task = mod.Task.query.one()
    assert task.id == task_id and task.description.splitlines() == list(updated)
    link = requests.model.query.filter_by(ticket_number=ticket).one()
    assert link.message_id == ORIGINAL_ID and link.reply_state == 'open'
    assert link.received_at == received.replace(tzinfo=None)
    assert link.revision_received_at == (received + timedelta(minutes=15)).replace(tzinfo=None)
    task.status = 'completed'
    mod.db.session.commit()
    assert sync(6, closed_id, 25)['duplicate'] == 1
    assert mod.Task.query.one().description.splitlines() == list(updated)


def test_inbox_links_matching_manual_task_and_keeps_its_author(completion, monkeypatch):
    mod, client, task_id = completion
    requests = mod.yandex_requests
    monkeypatch.setenv('YANDEX_MAIL_ENABLED', '1')
    submit = Mock()
    monkeypatch.setattr(requests, 'submit_reply', submit)
    manual = saved_task(completion)
    manual.description = 'Течь рулевой рейки\r\nСтук'
    manual.author_open = 'Приёмщик'
    mod.db.session.commit()

    received = datetime.now(MOSCOW) - timedelta(minutes=90)
    mail = fake_mail(received, {ORIGINAL_ID: mail_message(ORIGINAL_ID, 'УА160222071')})
    mail.unanswered.return_value = [[OriginalMail(b'1', ORIGINAL_ID, received, manual.board_number)]]
    args = mail, ADDRESS, {PARK: 'INBOX'}, None
    assert requests.import_requests(*args, apply=True)['linked'] == 1
    assert requests.import_requests(*args, apply=True)['duplicate'] == 1
    assert mod.Task.query.count() == 1
    assert saved_task(completion).author_open == 'Приёмщик'
    assert requests.model.query.filter_by(task_id=task_id).one_or_none().received_at == received.replace(tzinfo=None)
    assert 'УА160222071' in client.get(f'/complete_task/{task_id}').text

    check, _ = run_check(completion, monkeypatch)
    assert complete(completion, check['check_id'], completed_work=WORK).status_code == 200
    submit.assert_called_once_with(task_id, '164076')
    assert requests.model.query.filter_by(task_id=task_id).one_or_none().reply_text == WORK


def test_inbox_appends_new_defect_and_waits_for_board_before_importing_new_ticket(completion):
    mod, _, task_id = completion
    requests = mod.yandex_requests
    defects = ('Люки технологические, салона', 'Сидения салона', 'Фара ближнего света',
               'Освещение салона', 'ВК массы (Электросистема), не работает')
    extra = 'Отопитель (печка) салона, не работают в салоне'
    manual = saved_task(completion)
    manual.description = '\r\n'.join(defects)
    manual.author_open = 'Приёмщик'
    mod.db.session.commit()
    received = datetime.now(MOSCOW)
    second_id = '<another-ticket@example.org>'
    mail = fake_mail(received, {
        ORIGINAL_ID: mail_message(ORIGINAL_ID, 'УА160223263', defects=(*defects, extra)),
        second_id: mail_message(second_id, 'УА160223264', defects=defects, sender='another@example.org'),
    })
    mail.unanswered.return_value = [[OriginalMail(b'1', ORIGINAL_ID, received, '164076'),
                                     OriginalMail(b'2', second_id, received, '164076')]]

    args = mail, ADDRESS, {PARK: 'INBOX'}, None
    result = requests.import_requests(*args, apply=True)
    assert (result['linked'], result['created'], result['active_board']) == (1, 0, 1)
    assert requests.model.query.filter_by(task_id=task_id).one_or_none().ticket_number == 'УА160223263'
    assert saved_task(completion).description.splitlines() == [*defects, extra]
    assert mod.Task.query.count() == 1
    saved_task(completion).status = 'completed'
    mod.db.session.commit()
    assert requests.import_requests(*args, apply=True)['created'] == 1
    assert requests.model.query.filter_by(ticket_number='УА160223264').one().task_id != task_id


def test_inbox_matches_same_defects_when_one_position_has_a_typo(completion):
    mod, _, task_id = completion
    requests = mod.yandex_requests
    description = 'Датчик износа тормозных колодок, задняя левая\r\nЗамок технологического люка'
    manual = saved_task(completion)
    manual.description = description
    manual.author_open = 'Приёмщик'
    mod.db.session.commit()
    received = datetime.now(MOSCOW)
    mail = fake_mail(received, {
        ORIGINAL_ID: mail_message(ORIGINAL_ID, 'УА160223922', defects=(
            'Датчик износа тормозных колодок, заняя лева', 'Замок технологического люка',
        )),
    })
    mail.unanswered.return_value = [[OriginalMail(b'1', ORIGINAL_ID, received, '164076')]]

    result = requests.import_requests(mail, ADDRESS, {PARK: 'INBOX'}, None, apply=True)
    assert (result['linked'], result['created']) == (1, 0)
    assert requests.model.query.filter_by(task_id=task_id).one_or_none().ticket_number == 'УА160223922'
    assert saved_task(completion).description == description


@pytest.mark.parametrize('previous_link', [None, 'active', 'orphan', 'orphan_other_repair'])
def test_manual_closure_uses_latest_unanswered_subject(completion, monkeypatch, previous_link):
    mod, _, task_id = completion
    requests = mod.yandex_requests
    submit = Mock()
    monkeypatch.setattr(requests, 'submit_reply', submit)
    monkeypatch.setenv('YANDEX_MAIL_ENABLED', '1')
    monkeypatch.setenv('YANDEX_MAIL_FOLDERS', '{"14 парк":"INBOX"}')
    task = saved_task(completion)
    task.author_open = 'Приёмщик'
    if previous_link == 'orphan':
        task.description = 'Течь рулевой рейки\r\nСтук'
    mod.db.session.commit()
    check, _ = run_check(completion, monkeypatch)
    assert complete(completion, check['check_id'], completed_work=WORK).status_code == 200
    submit.assert_called_once_with(task_id, '164076')
    completed_at = saved_task(completion).completed_at.replace(tzinfo=MOSCOW)
    latest = '<latest@example.org>'
    replied = '<replied@example.org>'
    mail = fake_mail(completed_at, {latest: mail_message(latest, 'УА160222072')})
    latest_original = OriginalMail(b'2', latest, completed_at - timedelta(minutes=2), '164076')
    if previous_link:
        mail.unanswered.return_value = [[latest_original]]
        assert requests.import_requests(mail, ADDRESS, {PARK: 'INBOX'}, None, apply=True)['created'] == 1
        if previous_link.startswith('orphan'):
            old_task_id = requests.model.query.one().task_id
            mod.db.session.delete(mod.db.session.get(mod.Task, old_task_id))
            mod.db.session.commit()
    mail.unanswered.return_value = [[
        OriginalMail(b'1', ORIGINAL_ID, completed_at - timedelta(minutes=3), '164076'),
        latest_original,
        OriginalMail(b'3', replied, completed_at - timedelta(minutes=1), '164076'),
        OriginalMail(b'4', '<later@example.org>', completed_at + timedelta(minutes=1), '164076'),
    ]]
    mail.has_reply.side_effect = lambda message_id, _: message_id == replied

    monkeypatch.setenv('YANDEX_MAIL_IMPORT_FROM', (completed_at + timedelta(hours=2)).isoformat())
    requests.resolve_reply_lookups(mail, ADDRESS, {PARK: 'INBOX'}, task_id=task_id)
    mail.unanswered.assert_called_with('INBOX', None, board_number='164076')
    link = requests.model.query.filter_by(task_id=task_id).one_or_none()
    assert not saved_task(completion).yandex_reply_lookup
    if previous_link in ('active', 'orphan_other_repair'):
        assert link is None and requests.model.query.one().task_id != task_id
        assert requests.deliver_replies(mail, ADDRESS, task_id=task_id) == 0
        mail.send_reply.assert_not_called()
        return
    assert (link.message_id, link.reply_text, link.reply_state) == (latest, WORK, 'pending')
    assert requests.deliver_replies(mail, ADDRESS, task_id=task_id) == 1
    assert str(mail.send_reply.call_args.args[0]['In-Reply-To']) == latest


@pytest.mark.parametrize('linked_before_completion', [False, True])
def test_planned_maintenance_replies_to_matching_work_type(completion, monkeypatch, linked_before_completion):
    mod, _, task_id = completion
    requests = mod.yandex_requests
    monkeypatch.setenv('YANDEX_MAIL_ENABLED', '1')
    created = datetime(2026, 11, 23, 8, tzinfo=MOSCOW)
    task = saved_task(completion)
    task.created_at = created.replace(tzinfo=None)
    task.description = 'ТО2 300000'
    task.tag = 'to'
    task.id_to = '5228'
    task.author_open = 'Система'
    mod.db.session.commit()
    other_service = '<to1@example.org>'
    repair = '<repair@example.org>'
    expired = '<older-to2@example.org>'
    matching = OriginalMail(b'1', ORIGINAL_ID, created - timedelta(hours=8), '164076')
    old = OriginalMail(b'2', expired, created - timedelta(days=2), '164076')
    mail = fake_mail(created, {
        ORIGINAL_ID: mail_message(ORIGINAL_ID, 'УА160223571', service_type='Обслуживание техническое  (ТО-2)'),
        other_service: mail_message(other_service, 'УА160223572', service_type='ТО-1'),
        repair: mail_message(repair, 'УА160223573'),
        expired: mail_message(expired, 'УА160223574', service_type='ТО-2'),
    })
    folders = {PARK: 'INBOX'}
    mail.unanswered.return_value = [[matching, old,
        OriginalMail(b'3', other_service, created, '164076'),
        OriginalMail(b'4', repair, created + timedelta(minutes=1), '164076'),
    ]]
    if linked_before_completion:
        counts = requests.import_requests(mail, ADDRESS, folders, None, apply=True)
        assert (counts['created'], counts['linked']) == (0, 1)
        assert saved_task(completion).description == 'ТО2 300000'

    task = saved_task(completion)
    task.status = 'completed'
    task.completed_work = WORK
    task.completed_at = (created + timedelta(hours=12)).replace(tzinfo=None)
    queued = requests.queue_reply(task)
    if not queued:
        requests.queue_reply_lookup(task)
        assert task.yandex_reply_lookup
    mod.db.session.commit()
    monkeypatch.setenv('YANDEX_MAIL_IMPORT_FROM', '2026-11-24T00:00:00')
    requests.resolve_reply_lookups(mail, ADDRESS, folders, task_id=task_id)

    assert not saved_task(completion).yandex_reply_lookup
    link = requests.model.query.one()
    assert (link.task_id, link.ticket_number, link.reply_text) == (task_id, 'УА160223571', WORK)
    assert requests.deliver_replies(mail, ADDRESS, task_id=task_id) == 1
    assert str(mail.send_reply.call_args.args[0]['In-Reply-To']) == ORIGINAL_ID


@pytest.mark.parametrize('delivery', ['sent', 'answered', 'verify', 'pending', 'sent_copy', 'sent_mark', 'review'])
def test_completion_keeps_incontrol_check_and_sends_exact_reply(completion, monkeypatch, caplog, delivery):
    mod, client, old_task_id = completion
    requests = mod.yandex_requests
    executor = Mock()
    monkeypatch.setattr(requests, 'reply_executor', executor)
    monkeypatch.setenv('YANDEX_MAIL_ENABLED', '1')
    monkeypatch.setenv('YANDEX_MAIL_ADDRESS', ADDRESS)

    def check_committed(*args):
        assert not mod.db.session().in_transaction()

    executor.submit.side_effect = check_committed
    received = datetime.now(MOSCOW) - timedelta(hours=3)
    mail = fake_mail(received, {ORIGINAL_ID: mail_message(ORIGINAL_ID, 'УА160222071')})
    monkeypatch.setattr(requests, '_mailbox', lambda address, auth: nullcontext(mail))
    mail.unanswered.return_value = [[OriginalMail(b'1', ORIGINAL_ID, received, '164076')]]
    saved_task(completion).status = 'completed'
    mod.db.session.commit()
    result = mod.yandex_requests.import_requests(mail, ADDRESS, {PARK: 'INBOX'}, received.date(), apply=True)
    assert result['created'] == 1
    link = mod.yandex_requests.model.query.one()
    assert link.task_id != old_task_id
    task_case = mod, client, link.task_id
    assert 'УА160222071' in client.get(f'/complete_task/{link.task_id}').text

    complete(task_case, completed_work=WORK)
    assert saved_task(task_case).status == 'in_repair'
    assert saved_task(task_case).completed_work is None
    assert link.reply_state == 'open'
    executor.submit.assert_not_called()
    check, _ = run_check(task_case, monkeypatch)
    assert complete(task_case, check['check_id'], completed_work=WORK).status_code == 200
    assert saved_task(task_case).status == 'completed'
    assert link.reply_state == 'pending' and link.reply_text == WORK
    executor.submit.assert_called_once_with(requests._run_reply, task_case[2], '164076')

    if delivery == 'answered':
        mail.has_reply.return_value = True
    elif delivery == 'verify':
        mail.send_reply.side_effect = UncertainDelivery('Неясный результат SMTP')
    elif delivery == 'pending':
        mail.send_reply.side_effect = MailError('SMTP отклонил ответ')
    elif delivery == 'review':
        mail.send_reply.side_effect = PermanentDeliveryError('SMTP отклонил ответ (550).')
    elif delivery == 'sent_copy':
        mail.save_sent_copy.side_effect = ImapConnectionError('IMAP APPEND')
    elif delivery == 'sent_mark':
        mail.mark_answered.side_effect = ImapConnectionError('IMAP UID STORE')
    mod.db.session.remove()
    worker, task_id, board_number = executor.submit.call_args.args
    worker(task_id, board_number)
    stored = mod.yandex_requests.model.query.one()
    assert stored.reply_state == delivery
    event = {
        'answered': 'ответ уже есть',
        'verify': 'отправка не подтверждена',
        'pending': 'ответ остался в очереди',
        'review': 'требуется проверка в списке «К отписке»',
        'sent_copy': 'SMTP принял ответ, сбой IMAP (копия в отправленных)',
        'sent_mark': 'SMTP принял ответ, сбой IMAP (отметка на исходном письме)',
    }.get(delivery)
    if event:
        assert f'yandex-mail: борт=164076, task={task_id}, {event}' in caplog.text
    success = '14 парк 164076 ответ в почту отправлен'
    assert (success in caplog.text) == (delivery in ('sent', 'sent_copy', 'sent_mark'))

    if delivery == 'sent':
        reply = mail.send_reply.call_args.args[0]
        assert str(reply['To']) == 'repair@example.org'
        assert str(reply['In-Reply-To']) == ORIGINAL_ID == str(reply['References'])
        assert reply.get_content().rstrip('\n') == WORK
        copy = mail.save_sent_copy.call_args.args[0]
        assert copy['Message-ID'] == reply['Message-ID'] and copy.get_content() == reply.get_content()
        mail.mark_answered.assert_called_once_with('INBOX', ORIGINAL_ID, received.replace(tzinfo=None))
    elif delivery != 'sent_mark':
        mail.mark_answered.assert_not_called()
    if delivery == 'answered':
        mail.send_reply.assert_not_called()

    assert mod.yandex_requests.deliver_replies(mail, ADDRESS) == 0
    if delivery in ('pending', 'verify', 'sent_copy', 'sent_mark'):
        for method in (mail.send_reply, mail.save_sent_copy, mail.mark_answered):
            method.side_effect = None
        if delivery == 'verify':
            mail.has_sent_copy.return_value = True
        stored = requests.model.query.one()
        sent_at = stored.reply_sent_at
        stored.reply_next_at = utcnow() - timedelta(seconds=1)
        mod.db.session.commit()
        import_requests = Mock()
        monkeypatch.setattr(requests, 'import_requests', import_requests)
        assert requests.run(replies_only=True) is None
        import_requests.assert_not_called()
        stored = requests.model.query.one()
        assert stored.reply_state == 'sent'
        assert stored.reply_sent_at is not None
        assert mail.send_reply.call_count == (2 if delivery == 'pending' else 1)
        if delivery in ('sent_copy', 'sent_mark'):
            assert stored.reply_sent_at == sent_at
        if delivery in ('verify', 'sent_mark'):
            assert mail.save_sent_copy.call_count == (0 if delivery == 'verify' else 1)
    elif delivery == 'review':
        assert mod.incontrol_outbox.model.query.filter_by(task_id=task_id, service='yandex_mail', park=PARK).count() == 1


def test_scheduled_reply_check_preserves_in_flight_claim(app_ctx):
    mod = app_ctx
    requests = mod.yandex_requests
    mod.db.session.add(mod.Car(bort='164076', location=PARK))
    mod.db.session.commit()
    received = datetime.now(MOSCOW)
    mail = fake_mail(received, {ORIGINAL_ID: mail_message(ORIGINAL_ID, 'УА160222071')})
    assert requests.import_requests(mail, ADDRESS, {PARK: FOLDER}, None, apply=True)['created'] == 1
    task = mod.Task.query.one()
    task_id = task.id
    task.status = 'completed'
    task.completed_at = received.replace(tzinfo=None)
    task.completed_work = WORK
    assert requests.queue_reply(task)
    mod.db.session.commit()

    def fail_while_another_worker_sends(*args):
        assert requests._claim_reply(requests.model.query.filter_by(task_id=task_id).one().message_key)
        raise MailError('Не удалось проверить письмо')

    mail.original_answered.side_effect = fail_while_another_worker_sends
    assert requests.deliver_replies(mail, ADDRESS) == 1
    link = requests.model.query.filter_by(task_id=task_id).one_or_none()
    assert link.reply_state == 'sending' and link.reply_error is None
    assert requests.deliver_replies(mail, ADDRESS) == 0
    mail.send_reply.assert_not_called()

    link = requests.model.query.filter_by(task_id=task_id).one_or_none()
    link.reply_attempt_at = utcnow() - timedelta(minutes=10)
    mod.db.session.commit()
    mail.original_answered.side_effect = None
    assert requests.deliver_replies(mail, ADDRESS) == 1
    for delay in (1, 5, 15):
        link = requests.model.query.filter_by(task_id=task_id).one_or_none()
        assert link.reply_state == 'verify'
        assert abs((link.reply_next_at - utcnow()).total_seconds() - delay * 60) < 3
        link.reply_next_at = utcnow() - timedelta(seconds=1)
        mod.db.session.commit()
        assert requests.deliver_replies(mail, ADDRESS) == 1
    assert requests.model.query.filter_by(task_id=task_id).one_or_none().reply_state == 'review'
    assert requests.deliver_replies(mail, ADDRESS) == 0
    assert mod.incontrol_outbox.model.query.filter_by(task_id=task_id, service='yandex_mail', park=PARK).count() == 1
    mail.send_reply.assert_not_called()


def test_mail_replies_skip_parks_removed_from_folder_settings(app_ctx, monkeypatch):
    mod = app_ctx
    requests = mod.yandex_requests
    mod.db.session.add(mod.Car(bort='164076', location=PARK))
    mod.db.session.commit()
    received = datetime.now(MOSCOW)
    mail = fake_mail(received, {ORIGINAL_ID: mail_message(ORIGINAL_ID, 'УА160222071')})
    assert requests.import_requests(mail, ADDRESS, {PARK: FOLDER}, None, apply=True)['created'] == 1
    task = mod.Task.query.one()
    task.status = 'completed'
    task.completed_work = WORK
    assert requests.queue_reply(task)
    task_id = task.id
    mod.db.session.commit()

    monkeypatch.setenv('YANDEX_MAIL_FOLDERS', '{"1 парк":"Коньково"}')
    assert requests.deliver_replies(mail, ADDRESS) == 0
    assert requests.deliver_replies(mail, ADDRESS, task_id=task_id) == 0
    mail.send_reply.assert_not_called()
    assert requests.model.query.filter_by(task_id=task_id).one_or_none().reply_state == 'pending'


@pytest.mark.parametrize('author', [MAIL_TASK_AUTHOR, 'Приёмщик'])
def test_unlinked_task_can_exclude_unrelated_incontrol_application(rejection_page, monkeypatch, author):
    import incontrol_rejection_jobs as jobs

    mod, client, task, _, csrf = rejection_page
    task.author_open = author
    task.incontrol_task_id = None
    mod.db.session.commit()
    headers = {'X-CSRFToken': csrf, 'Accept': 'application/json'}
    monkeypatch.setattr(jobs, 'ensure_session', lambda client: None)
    monkeypatch.setattr(jobs, 'capture_session', lambda client: {})
    monkeypatch.setitem(mod.application.extensions['incontrol_accounts'], 'remember_auth', lambda uid, client: None)
    found = [{'id': 600, 'task_description': 'Другая заявка'}]
    monkeypatch.setattr(jobs, 'find_open_main_tasks', lambda *args: found)

    response = client.post(f'/complete_task/{task.id}/incontrol-plan', data={'main_outcome': 'complete'}, headers=headers)
    urls = response.json
    mod.incontrol_rejections.slots.acquire()
    mod.incontrol_rejections.worker(urls['run_id'], 'inspect', None)
    status = client.get(urls['status_url']).json
    assert status['state'] == 'choose' and status['allow_unrelated']

    response = client.post(urls['action_url'], data={'action': 'select', 'remote_id': 'unrelated'}, headers=headers)
    assert response.status_code == 202
    mod.incontrol_rejections.slots.acquire()
    mod.incontrol_rejections.worker(urls['run_id'], 'inspect', None)
    status = client.get(urls['status_url']).json
    assert status['state'] == 'plan' and status['unrelated_remote']
    assert task.incontrol_task_id is None
