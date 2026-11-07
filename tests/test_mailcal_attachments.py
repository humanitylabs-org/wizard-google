"""Drafts with attachments (contract 1.2): new files, keeping a draft's files on an in-place edit, forwarding files."""
import base64
import email
import email.policy
import unittest

import mailcal
from google import GMAIL


def b64(data):
    return base64.b64encode(data).decode()


def b64url(data):
    return base64.urlsafe_b64encode(data).decode().rstrip('=')


def parse(payload):
    raw = payload['message']['raw']
    return email.message_from_bytes(base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4)), policy=email.policy.default)


def files_of(message):
    return [(p.get_filename(), p.get_content_type(), p.get_payload(decode=True)) for p in message.iter_attachments()]


PDF = b'%PDF-1.4 tiny test pdf\n%%EOF\n'
OLD_PDF = b'%PDF-1.4 the old one\n' * 40


class FakeGmail:
    """Just enough Gmail: one draft r1 (message d1) with a PDF stored by attachment id and a small inline .txt."""

    def __init__(self):
        self.calls = []

    def payload(self, body='Old body', attach=True):
        parts = [{'mimeType': 'text/plain', 'filename': '', 'body': {'data': b64url(body.encode())}}]
        if attach:
            parts += [{'mimeType': 'application/pdf', 'filename': 'plan.pdf', 'body': {'attachmentId': 'ATT1', 'size': len(OLD_PDF)}},
                      {'mimeType': 'text/plain', 'filename': 'notes.txt', 'body': {'data': b64url(b'note text'), 'size': 9}}]
        return {'mimeType': 'multipart/mixed', 'headers': [], 'parts': parts}

    def __call__(self, method, url, query=None, body=None):
        self.calls.append((method, url, query, body))
        path = url[len(GMAIL):]
        headers = [{'name': 'To', 'value': 'Jay <jay@example.com>'}, {'name': 'Subject', 'value': 'Transition plan'},
                   {'name': 'In-Reply-To', 'value': '<orig@example.com>'}, {'name': 'References', 'value': '<orig@example.com>'},
                   {'name': 'Message-ID', 'value': '<d1@mail.gmail.com>'}]
        if method == 'GET' and path == '/drafts/r1':
            payload = self.payload()
            payload['headers'] = headers
            return {'id': 'r1', 'message': {'id': 'd1', 'threadId': 't1', 'payload': payload}}
        if method == 'GET' and path in ('/messages/d1/attachments/ATT1', '/messages/m9/attachments/ATT1'):
            return {'data': b64url(OLD_PDF), 'size': len(OLD_PDF)}
        if method == 'GET' and path == '/messages/m9':
            payload = self.payload('Forwarded')
            payload['headers'] = [{'name': 'Subject', 'value': 'Invoice'}]
            return {'id': 'm9', 'threadId': 't9', 'payload': payload}
        if method == 'GET' and path == '/messages/d1':
            payload = self.payload()
            payload['headers'] = headers
            return {'id': 'd1', 'threadId': 't1', 'labelIds': ['DRAFT'], 'payload': payload}
        if method == 'GET' and path == '/drafts':
            return {'drafts': [{'id': 'r0', 'message': {'id': 'zz'}}, {'id': 'r1', 'message': {'id': 'd1'}}]}
        if method == 'POST' and path == '/drafts':
            return {'id': 'r2', 'message': {'id': 'd2', 'threadId': 't2'}}
        if method == 'PUT' and path == '/drafts/r1':
            return {'id': 'r1', 'message': {'id': 'd3', 'threadId': 't1'}}
        raise AssertionError(f'unexpected {method} {url}')

    def written(self):
        return [c for c in self.calls if c[0] in ('POST', 'PUT')]


class Attachments(unittest.TestCase):
    def test_new_draft_carries_files(self):
        gmail = FakeGmail()
        out = mailcal.mail_draft(gmail, {'to': ['a@example.com'], 'subject': 'Hi', 'body': 'See attached.',
                                         'attachments': [{'name': 'Plan Ü.pdf', 'base64': b64(PDF)},
                                                         {'name': 'data.csv', 'mime': 'text/csv', 'base64': b64url(b'a,b\n1,2\n')}]})
        (method, url, _, payload), = gmail.written()
        self.assertEqual((method, url), ('POST', f'{GMAIL}/drafts'))
        message = parse(payload)
        self.assertEqual(message['To'], 'a@example.com')
        self.assertEqual(message.get_body(('plain',)).get_content().strip(), 'See attached.')
        self.assertEqual(files_of(message), [('Plan Ü.pdf', 'application/pdf', PDF), ('data.csv', 'text/csv', b'a,b\n1,2\n')])
        self.assertEqual(out['draft']['attachments'], [{'name': 'Plan Ü.pdf', 'mime': 'application/pdf', 'size': len(PDF)},
                                                       {'name': 'data.csv', 'mime': 'text/csv', 'size': 8}])
        self.assertFalse(out['sent'])

    def test_empty_body_allowed_with_a_file(self):
        gmail = FakeGmail()
        mailcal.mail_draft(gmail, {'to': ['a@example.com'], 'subject': 'File', 'attachments': [{'name': 'a.pdf', 'base64': b64(PDF)}]})
        self.assertEqual(len(files_of(parse(gmail.written()[0][3]))), 1)

    def test_edit_in_place_keeps_old_files(self):
        gmail = FakeGmail()
        out = mailcal.mail_draft(gmail, {'draft_id': 'r1', 'to': ['jay@example.com'], 'subject': 'Transition plan v2', 'body': 'New text',
                                         'attachments': [{'name': 'extra.pdf', 'base64': b64(PDF)}]})
        (method, url, _, payload), = gmail.written()
        self.assertEqual((method, url), ('PUT', f'{GMAIL}/drafts/r1'))
        self.assertEqual(payload['id'], 'r1')
        self.assertEqual(payload['message']['threadId'], 't1')
        message = parse(payload)
        self.assertEqual(message['Subject'], 'Transition plan v2')
        self.assertEqual(message['In-Reply-To'], '<orig@example.com>')
        self.assertEqual(message.get_body(('plain',)).get_content().strip(), 'New text')
        self.assertEqual(files_of(message), [('plan.pdf', 'application/pdf', OLD_PDF), ('notes.txt', 'text/plain', b'note text'),
                                             ('extra.pdf', 'application/pdf', PDF)])
        self.assertEqual(out['draft']['id'], 'r1')
        self.assertTrue(out['updated'])
        self.assertFalse(any(c[0] == 'POST' for c in gmail.calls))  # no second draft, nothing trashed

    def test_edit_in_place_removes_named_and_keeps_unpassed_fields(self):
        gmail = FakeGmail()
        mailcal.mail_draft(gmail, {'draft_id': 'r1', 'remove_attachments': ['notes.txt']})
        message = parse(gmail.written()[0][3])
        self.assertEqual(message['To'], 'jay@example.com')
        self.assertEqual(message['Subject'], 'Transition plan')
        self.assertEqual(message.get_body(('plain',)).get_content().strip(), 'Old body')  # the .txt attachment is not body text
        self.assertEqual([f[0] for f in files_of(message)], ['plan.pdf'])

    def test_forward_copies_files(self):
        gmail = FakeGmail()
        mailcal.mail_draft(gmail, {'to': ['b@example.com'], 'subject': 'Fwd: Invoice', 'body': 'fyi', 'attachments_from': 'm9',
                                   'remove_attachments': ['notes.txt']})
        message = parse(gmail.written()[0][3])
        self.assertEqual(files_of(message), [('plan.pdf', 'application/pdf', OLD_PDF)])

    def test_mail_get_names_the_draft(self):
        out = mailcal.mail_get(FakeGmail(), {'id': 'd1'})
        self.assertEqual(out['message']['draft_id'], 'r1')
        self.assertEqual([a['name'] for a in out['message']['attachments']], ['plan.pdf', 'notes.txt'])

    def test_bad_names_and_sizes_refused(self):
        for name in ('../etc/passwd', 'a/b.pdf', 'a\\b.pdf', '..', '', 'x\n.pdf', 'x' * 201):
            with self.assertRaises(mailcal.BadArgs, msg=name):
                mailcal.mail_draft(FakeGmail(), {'to': ['a@example.com'], 'subject': 's', 'attachments': [{'name': name, 'base64': b64(PDF)}]})
        with self.assertRaises(mailcal.BadArgs):
            mailcal.mail_draft(FakeGmail(), {'to': ['a@example.com'], 'subject': 's', 'attachments': [{'name': 'a.pdf', 'base64': '***'}]})
        with self.assertRaises(mailcal.BadArgs):
            mailcal.mail_draft(FakeGmail(), {'to': ['a@example.com'], 'subject': 's',
                                             'attachments': [{'name': f'{i}.pdf', 'base64': b64(PDF)} for i in range(11)]})
        big = b64(b'\0' * (6 * 1024 * 1024))
        with self.assertRaises(mailcal.BadArgs):
            mailcal.mail_draft(FakeGmail(), {'to': ['a@example.com'], 'subject': 's',
                                             'attachments': [{'name': f'{i}.bin', 'base64': big} for i in range(4)]})
        out = mailcal.new_attachments([{'name': 'x.pdf', 'mime': 'text/html; charset=x', 'base64': b64(PDF)},
                                       {'name': 'y.weird', 'mime': '<script>', 'base64': b64(PDF)}])
        self.assertEqual([f['mime'] for f in out], ['text/html', 'application/octet-stream'])

    def test_schema_offers_files(self):
        schema = next(t['inputSchema'] for t in mailcal.mcp_tools() if t['name'] == 'mail_draft')
        for key in ('attachments', 'draft_id', 'remove_attachments', 'attachments_from'):
            self.assertIn(key, schema['properties'])
        self.assertEqual(schema['required'], [])


if __name__ == '__main__':
    unittest.main()
