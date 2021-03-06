# -*- coding: utf-8 -*-

import datetime
import mock
import time

from django.test import override_settings
from django.utils.timezone import now as timezone_now

from zerver.lib.actions import create_stream_if_needed, do_create_user
from zerver.lib.digest import gather_new_streams, handle_digest_email, enqueue_emails, \
    gather_new_users
from zerver.lib.test_classes import ZulipTestCase
from zerver.models import get_client, get_realm, Realm, UserActivity, UserProfile

class TestDigestEmailMessages(ZulipTestCase):

    @mock.patch('zerver.lib.digest.enough_traffic')
    @mock.patch('zerver.lib.digest.send_future_email')
    def test_receive_digest_email_messages(self, mock_send_future_email: mock.MagicMock,
                                           mock_enough_traffic: mock.MagicMock) -> None:

        # build dummy messages for missed messages email reply
        # have Hamlet send Othello a PM. Othello will reply via email
        # Hamlet will receive the message.
        hamlet = self.example_user('hamlet')
        self.login(hamlet.email)
        result = self.client_post("/json/messages", {"type": "private",
                                                     "content": "test_receive_missed_message_email_messages",
                                                     "client": "test suite",
                                                     "to": self.example_email('othello')})
        self.assert_json_success(result)

        user_profile = self.example_user('othello')
        cutoff = time.mktime(datetime.datetime(year=2016, month=1, day=1).timetuple())

        handle_digest_email(user_profile.id, cutoff)
        self.assertEqual(mock_send_future_email.call_count, 1)

        kwargs = mock_send_future_email.call_args[1]
        self.assertEqual(kwargs['to_user_id'], user_profile.id)
        html = kwargs['context']['unread_pms'][0]['header']['html']
        expected_url = "'http://zulip.testserver/#narrow/pm-with/{id}-hamlet'".format(id=hamlet.id)
        self.assertIn(expected_url, html)

    @mock.patch('zerver.lib.digest.enough_traffic')
    @mock.patch('zerver.lib.digest.send_future_email')
    def test_huddle_urls(self, mock_send_future_email: mock.MagicMock,
                         mock_enough_traffic: mock.MagicMock) -> None:

        email = self.example_email('hamlet')
        self.login(email)

        huddle_emails = [
            self.example_email('cordelia'),
            self.example_email('othello'),
        ]

        payload = dict(
            type='private',
            content='huddle message',
            client='test suite',
            to=','.join(huddle_emails),
        )
        result = self.client_post("/json/messages", payload)
        self.assert_json_success(result)

        user_profile = self.example_user('othello')
        cutoff = time.mktime(datetime.datetime(year=2016, month=1, day=1).timetuple())

        handle_digest_email(user_profile.id, cutoff)
        self.assertEqual(mock_send_future_email.call_count, 1)

        kwargs = mock_send_future_email.call_args[1]
        self.assertEqual(kwargs['to_user_id'], user_profile.id)
        html = kwargs['context']['unread_pms'][0]['header']['html']

        other_user_ids = sorted([
            self.example_user('cordelia').id,
            self.example_user('hamlet').id,
        ])
        slug = ','.join(str(user_id) for user_id in other_user_ids) + '-group'
        expected_url = "'http://zulip.testserver/#narrow/pm-with/" + slug + "'"
        self.assertIn(expected_url, html)

    @mock.patch('zerver.lib.digest.queue_digest_recipient')
    @mock.patch('zerver.lib.digest.timezone_now')
    @override_settings(SEND_DIGEST_EMAILS=True)
    def test_inactive_users_queued_for_digest(self, mock_django_timezone: mock.MagicMock,
                                              mock_queue_digest_recipient: mock.MagicMock) -> None:
        cutoff = timezone_now()
        # Test Tuesday
        mock_django_timezone.return_value = datetime.datetime(year=2016, month=1, day=5)
        all_user_profiles = UserProfile.objects.filter(
            is_active=True, is_bot=False, enable_digest_emails=True)
        # Check that all users without an a UserActivity entry are considered
        # inactive users and get enqueued.
        enqueue_emails(cutoff)
        self.assertEqual(mock_queue_digest_recipient.call_count, all_user_profiles.count())
        mock_queue_digest_recipient.reset_mock()
        for realm in Realm.objects.filter(deactivated=False, digest_emails_enabled=True):
            user_profiles = all_user_profiles.filter(realm=realm)
            for user_profile in user_profiles:
                UserActivity.objects.create(
                    last_visit=cutoff - datetime.timedelta(days=1),
                    user_profile=user_profile,
                    count=0,
                    client=get_client('test_client'))
        # Check that inactive users are enqueued
        enqueue_emails(cutoff)
        self.assertEqual(mock_queue_digest_recipient.call_count, all_user_profiles.count())

    @mock.patch('zerver.lib.digest.queue_digest_recipient')
    @mock.patch('zerver.lib.digest.timezone_now')
    def test_disabled(self, mock_django_timezone: mock.MagicMock,
                      mock_queue_digest_recipient: mock.MagicMock) -> None:
        cutoff = timezone_now()
        # A Tuesday
        mock_django_timezone.return_value = datetime.datetime(year=2016, month=1, day=5)
        enqueue_emails(cutoff)
        mock_queue_digest_recipient.assert_not_called()

    @mock.patch('zerver.lib.digest.enough_traffic', return_value=True)
    @mock.patch('zerver.lib.digest.timezone_now')
    @override_settings(SEND_DIGEST_EMAILS=True)
    def test_active_users_not_enqueued(self, mock_django_timezone: mock.MagicMock,
                                       mock_enough_traffic: mock.MagicMock) -> None:
        cutoff = timezone_now()
        # A Tuesday
        mock_django_timezone.return_value = datetime.datetime(year=2016, month=1, day=5)
        realms = Realm.objects.filter(deactivated=False, digest_emails_enabled=True)
        for realm in realms:
            user_profiles = UserProfile.objects.filter(realm=realm)
            for counter, user_profile in enumerate(user_profiles, 1):
                UserActivity.objects.create(
                    last_visit=cutoff + datetime.timedelta(days=1),
                    user_profile=user_profile,
                    count=0,
                    client=get_client('test_client'))
        # Check that an active user is not enqueued
        with mock.patch('zerver.lib.digest.queue_digest_recipient') as mock_queue_digest_recipient:
            enqueue_emails(cutoff)
            self.assertEqual(mock_queue_digest_recipient.call_count, 0)

    @mock.patch('zerver.lib.digest.queue_digest_recipient')
    @mock.patch('zerver.lib.digest.timezone_now')
    @override_settings(SEND_DIGEST_EMAILS=True)
    def test_only_enqueue_on_valid_day(self, mock_django_timezone: mock.MagicMock,
                                       mock_queue_digest_recipient: mock.MagicMock) -> None:
        # Not a Tuesday
        mock_django_timezone.return_value = datetime.datetime(year=2016, month=1, day=6)

        # Check that digests are not sent on days other than Tuesday.
        cutoff = timezone_now()
        enqueue_emails(cutoff)
        self.assertEqual(mock_queue_digest_recipient.call_count, 0)

    @mock.patch('zerver.lib.digest.queue_digest_recipient')
    @mock.patch('zerver.lib.digest.timezone_now')
    @override_settings(SEND_DIGEST_EMAILS=True)
    def test_no_email_digest_for_bots(self, mock_django_timezone: mock.MagicMock,
                                      mock_queue_digest_recipient: mock.MagicMock) -> None:
        cutoff = timezone_now()
        # A Tuesday
        mock_django_timezone.return_value = datetime.datetime(year=2016, month=1, day=5)
        bot = do_create_user('some_bot@example.com', 'password', get_realm('zulip'), 'some_bot', '',
                             bot_type=UserProfile.DEFAULT_BOT)
        UserActivity.objects.create(
            last_visit=cutoff - datetime.timedelta(days=1),
            user_profile=bot,
            count=0,
            client=get_client('test_client'))

        # Check that bots are not sent emails
        enqueue_emails(cutoff)
        for arg in mock_queue_digest_recipient.call_args_list:
            user = arg[0][0]
            self.assertNotEqual(user.id, bot.id)

    @mock.patch('zerver.lib.digest.timezone_now')
    @override_settings(SEND_DIGEST_EMAILS=True)
    def test_new_stream_link(self, mock_django_timezone: mock.MagicMock) -> None:
        cutoff = datetime.datetime(year=2017, month=11, day=1)
        mock_django_timezone.return_value = datetime.datetime(year=2017, month=11, day=5)
        cordelia = self.example_user('cordelia')
        stream_id = create_stream_if_needed(cordelia.realm, 'New stream')[0].id
        new_stream = gather_new_streams(cordelia, cutoff)[1]
        expected_html = "<a href='http://zulip.testserver/#narrow/stream/{stream_id}-New-stream'>New stream</a>".format(stream_id=stream_id)
        self.assertIn(expected_html, new_stream['html'])

    @mock.patch('zerver.lib.digest.timezone_now')
    def test_gather_new_users(self, mock_django_timezone: mock.MagicMock) -> None:
        cutoff = timezone_now()
        do_create_user('abc@example.com', password='abc', realm=get_realm('zulip'), full_name='abc', short_name='abc')

        # Normal users get info about new users
        user = self.example_user('aaron')
        gathered_no_of_user, _ = gather_new_users(user, cutoff)
        self.assertEqual(gathered_no_of_user, 1)

        # Definitely, admin users get info about new users
        user = self.example_user('iago')
        gathered_no_of_user, _ = gather_new_users(user, cutoff)
        self.assertEqual(gathered_no_of_user, 1)

        # Guest users don't get info about new users
        user = self.example_user('polonius')
        gathered_no_of_user, _ = gather_new_users(user, cutoff)
        self.assertEqual(gathered_no_of_user, 0)

        # Zephyr users also don't get info about new users in their realm
        user = self.mit_user('starnine')
        do_create_user('abc@mit.edu', password='abc', realm=user.realm, full_name='abc', short_name='abc')
        gathered_no_of_user, _ = gather_new_users(user, cutoff)
        self.assertEqual(gathered_no_of_user, 0)
