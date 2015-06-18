__author__ = 'nico'

import logging
import asyncio
import threading
from urllib.parse import urlparse

from transitions import Machine, MachineError

from hbmqtt.utils import not_in_dict_or_none
from hbmqtt.session import Session, SessionState
from hbmqtt.mqtt.connect import ConnectPacket
from hbmqtt.mqtt.connack import ConnackPacket, ReturnCode
from hbmqtt.mqtt.packet import MQTTFixedHeader
from hbmqtt.mqtt.disconnect import DisconnectPacket
from hbmqtt.mqtt.pingreq import PingReqPacket
from hbmqtt.mqtt.pingresp import PingRespPacket
from hbmqtt.errors import NoDataException, HBMQTTException

_defaults = {
    'keep_alive': 60,
}


class ClientException(BaseException):
    pass

def gen_client_id():
    import uuid
    return str(uuid.uuid4())

class MQTTClient:
    states = ['new', 'connecting', 'connected', 'idle', 'disconnected']

    def __init__(self, client_id=None, config={}, loop=None):
        """

        :param config: Example yaml config
            broker:
                host: localhost
                port: 1883
                scheme: mqtt
                username: xxx
                password: yyy
                # OR
                uri: mqtt:xxx@yyy//localhost:1883/
                # OR a mix ot both
            keep_alive: 60
            clean_session: true
            will:
                retain: false
                topic: some/topic
                message: Will message
                qos: 0
        :param loop:
        :return:
        """
        self.logger = logging.getLogger(__name__)
        self.config = config.copy()
        self.config.update(_defaults)
        self._keep_alive = int(config.get('keep_alive', 60))
        if client_id is not None:
            self.client_id = client_id
        else:
            self.client_id = gen_client_id()
            self.logger.debug("Using generated client ID : %s" % self.client_id)

        self._init_states()
        if loop is not None:
            self._loop = loop
        else:
            self._loop = asyncio.get_event_loop()

        self._loop_thread = None
        self._session = None

    def _init_states(self):
        self.machine = Machine(states=MQTTClient.states, initial='new')
        self.machine.add_transition(trigger='connect', source='new', dest='connecting')
        self.machine.add_transition(trigger='connect', source='disconnected', dest='connecting')
        self.machine.add_transition(trigger='connect_fail', source='connecting', dest='disconnected')
        self.machine.add_transition(trigger='connect_success', source='connecting', dest='connected')
        self.machine.add_transition(trigger='idle', source='connected', dest='idle')
        self.machine.add_transition(trigger='disconnect', source='idle', dest='disconnected')
        self.machine.add_transition(trigger='disconnect', source='connected', dest='disconnected')

    def connect(self, **kwargs):
        try:
            self._loop.run_until_complete(self.connect_async(**kwargs))
        except Exception as e:
            self.logger.warn(e)

    @asyncio.coroutine
    def connect_async(self, host=None, port=None, username=None, password=None, uri=None, clean_session=None):
        try:
            self.machine.connect()
            self._session = self._init_session(host, port, username, password, uri, clean_session)
            self.logger.debug("Connect with session parameters: %s" % self._session)

            yield from self._connect_coro()
            self.machine.connect_success()
        except MachineError:
            msg = "Connect call incompatible with client current state '%s'" % self.machine.current_state
            self.logger.warn(msg)
            self.machine.connect_fail()
            raise ClientException(msg)
        except Exception as e:
            self.machine.connect_fail()
            self.logger.warn("Connection failed: %s " % e)
            raise ClientException("Connection failed: %s " % e)

    def disconnect(self):
        try:
            self.machine.disconnect()
            if self._loop.is_running():
                self._loop.call_soon(asyncio.async, self._disconnect_coro())
            else:
                self._loop.run_until_complete(self._disconnect_coro())
        except MachineError as me:
            self.logger.debug("Invalid method call at this moment: %s" % me)
            raise ClientException("Client instance can't be disconnected: %s" % me)
        self._loop.stop()
        self._session = None

    @asyncio.coroutine
    def _disconnect_coro(self):
        disconnect_packet = DisconnectPacket()
        yield from disconnect_packet.to_stream(self._session.writer)
        self._session.writer.close()

    def ping(self):
        self._loop.call_soon_threadsafe(asyncio.async, self._ping_coro())

    @asyncio.coroutine
    def _ping_coro(self):
        ping_packet = PingReqPacket()
        print(ping_packet)
        yield from ping_packet.to_stream(self._session.writer)
        response = yield from PingRespPacket.from_stream(self._session.reader)
        print(response)

    @asyncio.coroutine
    def _connect_coro(self):
        try:
            self._session.reader, self._session.writer = \
                yield from asyncio.open_connection(self._session.remote_address, self._session.remote_port)
            self._session.local_address, self._session.local_port = self._session.writer.get_extra_info('sockname')

            # Send CONNECT packet and wait for CONNACK
            packet = ConnectPacket.build_request_from_session(self._session)
            yield from packet.to_stream(self._session.writer)
            self.logger.debug(packet)
            connack = yield from ConnackPacket.from_stream(self._session.reader)
            self.logger.debug(connack)
            if connack.variable_header.return_code is not ReturnCode.CONNECTION_ACCEPTED:
                raise ClientException("Connection rejected with code '%s'" % hex(connack.variable_header.return_code))
            self._session.state = SessionState.CONNECTED
            self.logger.debug("connected to %s:%s" % (self._session.remote_address, self._session.remote_port))
        except Exception as e:
            self.logger.error("Connection failed: %s" % e)
            self._session.state = SessionState.DISCONNECTED
            raise

    def _init_session(self, host=None, port=None, username=None, password=None, uri=None, clean_session=None) -> dict:
        # Load config
        broker_conf = self.config.get('broker', dict()).copy()
        if 'mqtt' not in broker_conf:
            broker_conf['scheme'] = 'mqtt'
        if 'username' not in broker_conf:
            broker_conf['username'] = None
        if 'password' not in broker_conf:
            broker_conf['password'] = None

        if uri is not None:
            result = urlparse(uri)
            if result.scheme:
                broker_conf['scheme'] = result.scheme
            if result.hostname:
                broker_conf['host'] = result.hostname
            if result.port:
                broker_conf['port'] = result.port
            if result.username:
                broker_conf['username'] = result.username
            if result.password:
                broker_conf['password'] = result.password
        if host:
            broker_conf['host'] = host
        if port:
            broker_conf['port'] = int(port)
        if username:
            broker_conf['username'] = username
        if password:
            broker_conf['password'] = password
        if clean_session is not None:
            broker_conf['clean_session'] = clean_session

        for key in ['scheme', 'host', 'port']:
            if not_in_dict_or_none(broker_conf, key):
                raise ClientException("Missing connection parameter '%s'" % key)

        s = Session()
        s.client_id = self.client_id
        s.remote_address = broker_conf['host']
        s.remote_port = broker_conf['port']
        s.username = broker_conf['username']
        s.password = broker_conf['password']
        s.scheme = broker_conf['scheme']
        if clean_session is not None:
            s.clean_session = clean_session
        else:
            s.clean_session = self.config.get('clean_session', True)
        s.keep_alive = self.config['keep_alive']
        if 'will' in self.config:
            s.will_flag = True
            s.will_retain = self.config['will']['retain']
            s.will_topic = self.config['will']['topic']
            s.will_message = self.config['will']['message']
        else:
            s.will_flag = False
            s.will_retain = False
            s.will_topic = None
            s.will_message = None
        return s

