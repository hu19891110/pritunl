from pritunl.exceptions import *
from pritunl.constants import *
from pritunl import logger
from pritunl import settings
from pritunl import sso
from pritunl import plugins

import threading

class Authorizer(object):
    __slots__ = (
        'server',
        'user',
        'remote_ip',
        'platform',
        'device_name',
        'password',
        'reauth',
        'callback',
        'push_type',
    )

    def __init__(self, svr, usr, remote_ip, plaform, device_name,
            password, reauth, callback):
        self.server = svr
        self.user = usr
        self.remote_ip = remote_ip
        self.platform = plaform
        self.device_name = device_name
        self.password = password
        self.reauth = reauth
        self.callback = callback
        self.push_type = None

    def _set_push_type(self):
        if settings.app.sso and DUO_AUTH in self.user.auth_type and \
                DUO_AUTH in settings.app.sso:
            self.push_type = DUO_AUTH
        elif settings.app.sso and \
                SAML_OKTA_AUTH in self.user.auth_type and \
                SAML_OKTA_AUTH in settings.app.sso:
            self.push_type = SAML_OKTA_AUTH

    def authenticate(self):
        try:
            self._check_call(self._check_primary)
            self._check_call(self._check_password)
            self._check_call(self._check_sso)
            self._check_call(self._auth_plugins)
            if not self.reauth:
                self._check_call(self._check_push)
            self.callback(True)
        except:
            pass

    def _check_call(self, func):
        try:
            func()
        except AuthError, err:
            self.callback(False, str(err))
            raise
        except AuthForked:
            raise
        except:
            logger.exception('Exception in user authorize', 'authorize')
            self.callback(False, 'Unknown error occured')
            raise

    def _check_primary(self):
        if not self.server.check_groups(self.user.groups):
            self.user.audit_event(
                'user_connection',
                ('User connection to "%s" denied. User not in ' +
                    'servers groups') % (self.server.name),
                remote_addr=self.remote_ip,
            )
            raise AuthError('User not in servers groups')

        if self.user.disabled:
            self.user.audit_event('user_connection',
                'User connection to "%s" denied. User is disabled' % (
                    self.server.name),
                remote_addr=self.remote_ip,
            )
            raise AuthError('User is disabled')

    def _check_password(self):
        if self.user.bypass_secondary or settings.vpn.stress_test:
            return

        if self.server.otp_auth and self.user.type == CERT_CLIENT:
            otp_code = self.password[-6:]
            self.password = self.password[:-6]

            if not self.user.verify_otp_code(otp_code, self.remote_ip):
                self.user.audit_event('user_connection',
                    ('User connection to "%s" denied. ' +
                     'User failed two-step authentication') % (
                        self.server.name),
                    remote_addr=self.remote_ip,
                )
                raise AuthError('Invalid OTP code')

        if self.user.pin and settings.user.pin_mode != PIN_DISABLED:
            if not self.user.check_pin(self.password):
                self.user.audit_event('user_connection',
                    ('User connection to "%s" denied. ' +
                     'User failed pin authentication') % (
                        self.server.name),
                    remote_addr=self.remote_ip,
                )
                raise AuthError('Invalid pin')
        elif settings.user.pin_mode == PIN_REQUIRED:
            self.user.audit_event('user_connection',
                ('User connection to "%s" denied. ' +
                 'User does not have a pin set') % (
                    self.server.name),
                remote_addr=self.remote_ip,
            )
            raise AuthError('User does not have a pin set')

    def _check_sso(self):
        if self.user.bypass_secondary or settings.vpn.stress_test:
            return

        if not self.user.sso_auth_check(self.password, self.remote_ip):
            self.user.audit_event('user_connection',
                ('User connection to "%s" denied. ' +
                 'Single sign-on authentication failed') % (
                    self.server.name),
                remote_addr=self.remote_ip,
            )
            raise AuthError('Failed secondary authentication')

    def _check_push(self):
        if self.user.bypass_secondary or settings.vpn.stress_test:
            return

        self._set_push_type()
        if not self.push_type:
            return

        def thread_func():
            try:
                self._check_call(self._auth_push_thread)
            except:
                return
            self.callback(True)

        thread = threading.Thread(target=thread_func)
        thread.daemon = True
        thread.start()

        raise AuthForked()

    def _auth_push_thread(self):
        info={
            'Server': self.server.name,
        }

        platform_name = None
        if self.platform == 'linux':
            platform_name = 'Linux'
        elif self.platform == 'mac' or self.platform == 'ios':
            platform_name = 'Apple'
        elif self.platform == 'win':
            platform_name = 'Windows'
        elif self.platform == 'chrome':
            platform_name = 'Chrome OS'

        if self.device_name:
            info['Device'] = '%s (%s)' % (self.device_name, platform_name)

        if self.push_type == DUO_AUTH:
            allow, _ = sso.auth_duo(
                self.user.name,
                ipaddr=self.remote_ip,
                type='Connection',
                info=info,
            )
        elif self.push_type == SAML_OKTA_AUTH:
            allow = sso.auth_okta_push(
                self.user.name,
                ipaddr=self.remote_ip,
                type='Connection',
                info=info,
            )
        else:
            raise ValueError('Unkown push auth type')

        if not allow:
            self.user.audit_event('user_connection',
                ('User connection to "%s" denied. ' +
                 'Push authentication failed') % (
                    self.server.name),
                remote_addr=self.remote_ip,
            )
            raise AuthError('User failed push authentication')

    def _auth_plugins(self):
        if self.user.type == CERT_CLIENT:
            returns = plugins.caller(
                'user_connect',
                host_id=settings.local.host_id,
                server_id=self.server.id,
                org_id=self.user.org.id,
                user_id=self.user.id,
                host_name=settings.local.host.name,
                server_name=self.server.name,
                org_name=self.user.org.name,
                user_name=self.user.name,
                remote_ip=self.remote_ip,
                platform=self.platform,
                device_name=self.device_name,
                password=self.password,
            )

            if not returns:
                return

            for return_val in returns:
                if not return_val[0]:
                    raise AuthError(return_val[1])
