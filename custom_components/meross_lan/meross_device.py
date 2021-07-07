from enum import Enum
from io import TextIOWrapper
import logging
import math
import os
from typing import  Callable, Dict
from time import strftime, time
from logging import INFO, WARNING, DEBUG
from aiohttp.client_exceptions import ClientConnectionError, ClientConnectorError
from json import (
    dumps as json_dumps,
    loads as json_loads,
)

from homeassistant.helpers.config_validation import path
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.config_entries import ConfigEntries, ConfigEntry
from homeassistant.core import HomeAssistant, callback

from .logger import LOGGER, LOGGER_trap

from .const import (
    DOMAIN,
    CONF_DEVICE_ID, CONF_KEY, CONF_PAYLOAD, CONF_HOST, CONF_TIMESTAMP,
    CONF_POLLING_PERIOD, CONF_POLLING_PERIOD_DEFAULT, CONF_POLLING_PERIOD_MIN,
    CONF_PROTOCOL, CONF_OPTION_AUTO, CONF_OPTION_HTTP, CONF_OPTION_MQTT,
    CONF_TIME_ZONE,
    CONF_TRACE, CONF_TRACE_DIRECTORY, CONF_TRACE_FILENAME, CONF_TRACE_MAXSIZE,
    PARAM_UNAVAILABILITY_TIMEOUT, PARAM_HEARTBEAT_PERIOD
)

from .merossclient import KeyType, MerossDeviceDescriptor, MerossHttpClient, const as mc  # mEROSS cONST

# these are dynamically created MerossDevice attributes in a sort of a dumb optimization
VOLATILE_ATTR_HTTPCLIENT = '_httpclient'
VOLATILE_ATTR_TRACEFILE = '_tracefile'
VOLATILE_ATTR_TRACEENDTIME = '_traceendtime'

class Protocol(Enum):
    """
    Describes the protocol selection behaviour in order to connect to devices
    """
    AUTO = 0 # 'best effort' behaviour
    MQTT = 1
    HTTP = 2


MAP_CONF_PROTOCOL = {
    CONF_OPTION_AUTO: Protocol.AUTO,
    CONF_OPTION_MQTT: Protocol.MQTT,
    CONF_OPTION_HTTP: Protocol.HTTP
}


class MerossDevice:

    def __init__(
        self,
        api: object,
        descriptor: MerossDeviceDescriptor,
        entry: ConfigEntry
    ):
        self.device_id = entry.data.get(CONF_DEVICE_ID)
        LOGGER.debug("MerossDevice(%s) init", self.device_id)
        self.api = api
        self.descriptor = descriptor
        self.entry_id = entry.entry_id
        self.replykey = None
        self._online = False
        self.needsave = False # while parsing ns.ALL code signals to persist ConfigEntry
        self._retry_period = 0 # used to try reconnect when falling offline
        self.lastpoll = 0
        self.lastrequest = 0
        self.lastupdate = 0
        self.lastmqtt = 0
        """
        self.entities: dict()
        is a collection of all of the instanced entities
        they're generally built here during __init__ and will be registered
        in platforms(s) async_setup_entry with HA
        """
        self.entities: Dict[any, '_MerossEntity'] = dict()  # pylint: disable=undefined-variable

        """
        This is mainly for HTTP based devices: we build a dictionary of what we think could be
        useful to asynchronously poll so the actual polling cycle doesnt waste time in checks
        TL:DR we'll try to solve everything with just NS_SYS_ALL since it usually carries the full state
        in a single transaction. Also (see #33) the multiplug mss425 doesnt publish the full switch list state
        through NS_CNTRL_TOGGLEX (not sure if it's the firmware or the dialect)
        As far as we know rollershutter digest doesnt report state..so we'll add requests for that
        For Hub(s) use a 'dedicated' poll structure and don't use NS_ALL at all since it is bulky and doesnt
        carry all the relevant status info (at least MTS100 state is not fully exposed)
        """
        self.polling_period = CONF_POLLING_PERIOD_DEFAULT
        self.polling_dictionary = dict()
        ability = self.descriptor.ability
        if (mc.KEY_HUB not in descriptor.digest):
            self.polling_dictionary[mc.NS_APPLIANCE_SYSTEM_ALL] = {} # default
        if mc.NS_APPLIANCE_ROLLERSHUTTER_POSITION in ability:
            self.polling_dictionary[mc.NS_APPLIANCE_ROLLERSHUTTER_POSITION] = { mc.KEY_POSITION : [] }
        if mc.NS_APPLIANCE_ROLLERSHUTTER_STATE in ability:
            self.polling_dictionary[mc.NS_APPLIANCE_ROLLERSHUTTER_STATE] = { mc.KEY_STATE : [] }


        """
        self.platforms: dict()
        when we build an entity we also add the relative platform name here
        so that the async_setup_entry for the integration will be able to forward
        the setup to the appropriate platform.
        The item value here will be set to the async_add_entities callback
        during the corresponding platform async_setup_entry so to be able
        to dynamically add more entities should they 'pop-up' (Hub only?)
        """
        self.platforms: Dict[str, Callable] = {}
        """
        misc callbacks
        """
        self.unsub_entry_update_listener: Callable = None
        self.unsub_updatecoordinator_listener: Callable = None

        self._set_config_entry(entry.data)

        """
        warning: would the response be processed after this object is fully init?
        It should if I get all of this async stuff right
        also: !! IMPORTANT !! don't send any other message during init process
        else the responses could overlap and 'fuck' a bit the offline -> online transition
        causing that code to request a new NS_APPLIANCE_SYSTEM_ALL
        """
        self.request(mc.NS_APPLIANCE_SYSTEM_ALL)


    def __del__(self):
        LOGGER.debug("MerossDevice(%s) destroy", self.device_id)
        return


    @property
    def online(self) -> bool:
        if self._online:
            #evaluate device MQTT availability by checking lastrequest got answered in less than polling_period
            if (self.lastupdate > self.lastrequest) or ((time() - self.lastrequest) < self.polling_period):
                return True

            # when we 'fall' offline while on MQTT eventually retrigger HTTP.
            # the reverse is not needed since we switch HTTP -> MQTT right-away
            # when HTTP fails (see async_http_request)
            if (self.curr_protocol is Protocol.MQTT) and (self.conf_protocol is Protocol.AUTO):
                self._switch_protocol(Protocol.HTTP)
                return True

            self._set_offline()

        return False


    def receive(
        self,
        namespace: str,
        method: str,
        payload: dict,
        replykey: KeyType
    ) -> bool:
        """
        every time we receive a response we save it's 'replykey':
        that would be the same as our self.key (which it is compared against in 'get_replykey')
        if it's good else it would be the device message header to be used in
        a reply scheme where we're going to 'fool' the device by using its own hashes
        if our config allows for that (our self.key is 'None' which means empty key or auto-detect)

        Update: this key trick actually doesnt work on MQTT (but works on HTTP)
        """
        self.replykey = replykey
        if self.key and (replykey != self.key):
            self.log(WARNING, 14400, "Meross device key error for device_id: %s", self.device_id)

        self.lastupdate = time()
        if not self._online:
            if namespace != mc.NS_APPLIANCE_SYSTEM_ALL:
                self.request(mc.NS_APPLIANCE_SYSTEM_ALL)
            self._set_online()

        if namespace == mc.NS_APPLIANCE_CONTROL_TOGGLEX:
            self._parse_togglex(payload.get(mc.KEY_TOGGLEX))
            return True

        if namespace == mc.NS_APPLIANCE_SYSTEM_ALL:
            self._parse_all(payload)
            if self.needsave is True:
                self.needsave = False
                self._save_config_entry(payload)
            return True

        return False


    def mqtt_receive(
        self,
        namespace: str,
        method: str,
        payload: dict,
        replykey: KeyType
    ) -> None:
        if self.conf_protocol is Protocol.HTTP:
            return # even if mqtt parsing is no harming we want a 'consistent' HTTP only behaviour
        self._trace(payload, namespace, method, CONF_OPTION_MQTT)
        self.lastmqtt = time()
        if (self.pref_protocol is Protocol.MQTT) and (self.curr_protocol is Protocol.HTTP):
            self._switch_protocol(Protocol.MQTT)
        self.receive(namespace, method, payload, replykey)


    async def async_http_request(self, namespace: str, method: str, payload: dict = {}, callback: Callable = None):
        try:
            _httpclient:MerossHttpClient = getattr(self, VOLATILE_ATTR_HTTPCLIENT, None)
            if _httpclient is None:
                _httpclient = MerossHttpClient(self.descriptor.innerIp, self.key, async_get_clientsession(self.api.hass), LOGGER)
                self._httpclient = _httpclient
            else:
                _httpclient.set_host_key(self.descriptor.innerIp, self.key)

            self._trace(payload, namespace, method, CONF_OPTION_HTTP)
            try:
                response = await _httpclient.async_request(namespace, method, payload)
            except Exception as e:
                if self._online:
                    self.log(INFO, 0, "MerossDevice(%s) client connection error in async_http_request: %s", self.device_id, str(e) or type(e).__name__)
                    if (self.conf_protocol is Protocol.AUTO) and self.lastmqtt:
                        self.lastmqtt = 0
                        self._switch_protocol(Protocol.MQTT)
                        self._trace(payload, namespace, method, CONF_OPTION_MQTT)
                        self.api.mqtt_publish(
                            self.device_id,
                            namespace,
                            method,
                            payload,
                            self.key or self.replykey
                            )
                    else:
                        self._set_offline()
                return

            r_header = response[mc.KEY_HEADER]
            r_namespace = r_header[mc.KEY_NAMESPACE]
            r_method = r_header[mc.KEY_METHOD]
            r_payload = response[mc.KEY_PAYLOAD]
            self._trace(r_payload, r_namespace, r_method, CONF_OPTION_HTTP)
            if (callback is not None) and (r_method == mc.METHOD_SETACK):
                #we're actually only using this for SET->SETACK command confirmation
                callback()
            # passing self.key to shut off MerossDevice replykey behaviour
            # since we're already managing replykey in http client
            self.receive(r_namespace, r_method, r_payload, self.key)
        except Exception as e:
            self.log(WARNING, 14400, "MerossDevice(%s) error in async_http_request: %s", self.device_id, str(e) or type(e).__name__)


    def request(self, namespace: str, method: str = mc.METHOD_GET, payload: dict = {}, callback: Callable = None):
        """
            route the request through MQTT or HTTP to the physical device.
            callback will be called on successful replies and actually implemented
            only when HTTPing SET requests. On MQTT we rely on async PUSH and SETACK to manage
            confirmation/status updates
        """
        self.lastrequest = time()
        if self.curr_protocol is Protocol.HTTP:
            self.api.hass.async_create_task(
                self.async_http_request(namespace, method, payload, callback)
            )
        else: # self.curr_protocol is Protocol.MQTT:
            self._trace(payload, namespace, method, CONF_OPTION_MQTT)
            self.api.mqtt_publish(
                self.device_id,
                namespace,
                method,
                payload,
                self.key or self.replykey
            )


    def _parse_togglex(self, payload) -> None:
        if isinstance(payload, dict):
            self.entities[payload.get(mc.KEY_CHANNEL, 0)]._set_onoff(payload.get(mc.KEY_ONOFF))
        elif isinstance(payload, list):
            for p in payload:
                self._parse_togglex(p)


    def _parse_all(self, payload: dict) -> None:
        """
        called internally when we receive an NS_SYSTEM_ALL
        i.e. global device setup/status
        we usually don't expect a 'structural' change in the device here
        except maybe for Hub(s) which we're going to investigate later
        Return True if we want to persist the payload to the ConfigEntry
        """
        descr = self.descriptor
        oldaddr = descr.innerIp
        descr.update(payload)
        #persist changes to configentry only when relevant properties change
        if oldaddr != descr.innerIp:
            self.needsave = True

        if self.time_zone and (descr.timezone != self.time_zone):
            self.request(
                mc.NS_APPLIANCE_SYSTEM_TIME,
                mc.METHOD_SET,
                payload={mc.KEY_TIME: {mc.KEY_TIMEZONE: self.time_zone}}
                )

        for key, value in descr.digest.items():
            _parse = getattr(self, f"_parse_{key}", None)
            if _parse is not None:
                _parse(value)


    def _set_offline(self) -> None:
        self.log(DEBUG, 0, "MerossDevice(%s) going offline!", self.device_id)
        self._online = False
        self._retry_period = 0
        for entity in self.entities.values():
            entity._set_unavailable()


    def _set_online(self) -> None:
        """
            When coming back online allow for a refresh
            also in inheriteds
        """
        self.log(DEBUG, 0, "MerossDevice(%s) back online!", self.device_id)
        self._online = True
        self.updatecoordinator_listener()


    def _switch_protocol(self, protocol: Protocol) -> None:
        self.log(INFO, 0, "MerossDevice(%s) switching protocol to %s", self.device_id, protocol.name)
        self.curr_protocol = protocol


    def _save_config_entry(self, payload: dict) -> None:
        try:
            entries:ConfigEntries = self.api.hass.config_entries
            entry:ConfigEntry = entries.async_get_entry(self.entry_id)
            if entry is not None:
                data = dict(entry.data) # deepcopy? not needed: see CONF_TIMESTAMP
                data[CONF_PAYLOAD].update(payload)
                data[CONF_TIMESTAMP] = time() # force ConfigEntry update..
                entries.async_update_entry(entry, data=data)
        except Exception as e:
            self.log(WARNING, 0, "MerossDevice(%s) error while updating ConfigEntry (%s)", self.device_id, str(e))


    def _set_config_entry(self, data: dict) -> None:
        """
        common properties read from ConfigEntry on __init__ or when a configentry updates
        """
        self.key = data.get(CONF_KEY)
        self.conf_protocol = MAP_CONF_PROTOCOL.get(data.get(CONF_PROTOCOL), Protocol.AUTO)
        if self.conf_protocol == Protocol.AUTO:
            self.pref_protocol = Protocol.HTTP if data.get(CONF_HOST) else Protocol.MQTT
        else:
            self.pref_protocol = self.conf_protocol
        """
        When using Protocol.AUTO we try to use our 'preferred' (pref_protocol)
        and eventually fallback (curr_protocol) until some good news allow us
        to retry pref_protocol
        """
        self.curr_protocol = self.pref_protocol
        self.lastmqtt = 0
        self.polling_period = data.get(CONF_POLLING_PERIOD, CONF_POLLING_PERIOD_DEFAULT)
        if self.polling_period < CONF_POLLING_PERIOD_MIN:
            self.polling_period = CONF_POLLING_PERIOD_MIN

        self.time_zone = data.get(CONF_TIME_ZONE) # TODO: add to config_flow options


    @callback
    async def entry_update_listener(self, hass: HomeAssistant, config_entry: ConfigEntry) -> None:
        # we're not changing device_id or other 'identifying' stuff
        self._set_config_entry(config_entry.data)
        self.api.update_polling_period()
        _httpclient:MerossHttpClient = getattr(self, VOLATILE_ATTR_HTTPCLIENT, None)
        if _httpclient is not None:
            _httpclient.set_host_key(self.descriptor.innerIp, self.key)
        """
        We'll activate debug tracing only when the user turns it on in OptionsFlowHandler so we usually
        don't care about it on startup ('_set_config_entry'). When updating ConfigEntry
        we always reset the timeout and so the trace will (eventually) restart
        """
        _tracefile: TextIOWrapper = getattr(self, VOLATILE_ATTR_TRACEFILE, None)
        if _tracefile is not None:
            self._trace_close(_tracefile)
        _traceendtime = config_entry.data.get(CONF_TRACE, 0)
        if _traceendtime > time():
            try:
                tracedir = hass.config.path('custom_components', DOMAIN, CONF_TRACE_DIRECTORY)
                os.makedirs(tracedir, exist_ok=True)
                self._tracefile = open(os.path.join(tracedir, CONF_TRACE_FILENAME.format(self.descriptor.type, self.device_id)), 'w')
                self._traceendtime = _traceendtime
                self._trace(self.descriptor.all, mc.NS_APPLIANCE_SYSTEM_ALL, mc.METHOD_GETACK)
                self._trace(self.descriptor.ability, mc.NS_APPLIANCE_SYSTEM_ABILITY, mc.METHOD_GETACK)
            except Exception as e:
                LOGGER.warning("MerossDevice(%s) error while creating trace file (%s)", self.device_id, str(e))

        #await hass.config_entries.async_reload(config_entry.entry_id)


    def _trace_close(self, tracefile: TextIOWrapper):
        try:
            delattr(self, VOLATILE_ATTR_TRACEFILE)
            delattr(self, VOLATILE_ATTR_TRACEENDTIME)
            tracefile.close()
        except Exception as e:
            LOGGER.warning("MerossDevice(%s) error while closing trace file (%s)", self.device_id, str(e))


    def _trace(self, data, namespace = '', method = '', protocol = CONF_OPTION_AUTO):
        _tracefile: TextIOWrapper = getattr(self, VOLATILE_ATTR_TRACEFILE, None)
        if _tracefile is not None:
            now = time()
            _traceendtime = getattr(self, VOLATILE_ATTR_TRACEENDTIME, 0)
            if now > _traceendtime:
                self._trace_close(_tracefile)
                return

            if namespace == mc.NS_APPLIANCE_SYSTEM_ALL:
                all = data.get(mc.KEY_ALL, data)
                system = all.get(mc.KEY_SYSTEM, {})
                hardware = system.get(mc.KEY_HARDWARE, {})
                firmware = system.get(mc.KEY_FIRMWARE, {})
                obfuscated = dict()
                obfuscated[mc.KEY_MACADDRESS] = hardware.get(mc.KEY_MACADDRESS)
                hardware[mc.KEY_MACADDRESS] = mc.MEROSS_MACADDRESS
                obfuscated[mc.KEY_WIFIMAC] = firmware.get(mc.KEY_WIFIMAC)
                firmware[mc.KEY_WIFIMAC] = mc.MEROSS_MACADDRESS
                obfuscated[mc.KEY_INNERIP] = firmware.get(mc.KEY_INNERIP)
                firmware[mc.KEY_INNERIP] = 'XXX.XXX.XXX.XXX'
                obfuscated[mc.KEY_SERVER] = firmware.get(mc.KEY_SERVER)
                firmware[mc.KEY_SERVER] = firmware[mc.KEY_INNERIP]
                obfuscated[mc.KEY_PORT] = firmware.get(mc.KEY_PORT)
                firmware[mc.KEY_PORT] = ''
                obfuscated[mc.KEY_USERID] = firmware.get(mc.KEY_USERID)
                firmware[mc.KEY_USERID] = ''

            try:
                _tracefile.write(strftime('%Y/%m/%d - %H:%M:%S\t') \
                    + protocol + '\t' + method + '\t' + namespace + '\t' \
                    + (json_dumps(data) if isinstance(data, dict) else data) + '\r\n')
                if _tracefile.tell() > CONF_TRACE_MAXSIZE:
                    self._trace_close(_tracefile)
            except Exception as e:
                LOGGER.warning("MerossDevice(%s) error while writing to trace file (%s)", self.device_id, str(e))
                self._trace_close(_tracefile)

            if namespace == mc.NS_APPLIANCE_SYSTEM_ALL:
                hardware[mc.KEY_MACADDRESS] = obfuscated.get(mc.KEY_MACADDRESS)
                firmware[mc.KEY_WIFIMAC] = obfuscated.get(mc.KEY_WIFIMAC)
                firmware[mc.KEY_INNERIP] = obfuscated.get(mc.KEY_INNERIP)
                firmware[mc.KEY_SERVER] = obfuscated.get(mc.KEY_SERVER)
                firmware[mc.KEY_PORT] = obfuscated.get(mc.KEY_PORT)
                firmware[mc.KEY_USERID] = obfuscated.get(mc.KEY_USERID)


    def log(self, level: int, timeout: int, msg: str, *args):
        if timeout:
            LOGGER_trap(level, timeout, msg, *args)
        else:
            LOGGER.log(level, msg, *args)
        self._trace(msg % args, logging.getLevelName(level), 'LOG')


    @callback
    def updatecoordinator_listener(self) -> bool:
        now = time()
        """
        this is a bit rude: we'll keep sending 'heartbeats'
        to check if the device is still there
        !!this is mainly for MQTT mode since in HTTP we'll more or less poll
        unless the device went offline so we started skipping polling updates
        """
        if ((now - self.lastrequest) > PARAM_HEARTBEAT_PERIOD) \
            and ((now - self.lastupdate) > PARAM_HEARTBEAT_PERIOD):
            self.request(mc.NS_APPLIANCE_SYSTEM_ALL)
            return False # prevent any other poll action...

        if self.online:

            if (now - self.lastpoll) < self.polling_period:
                return False

            self.lastpoll = math.floor(now)

            # on MQTT we already have PUSHES...
            if (self.curr_protocol == Protocol.HTTP) and ((now - self.lastmqtt) > PARAM_HEARTBEAT_PERIOD):
                for namespace, payload in self.polling_dictionary.items():
                    self.request(namespace, payload=payload)
            return True # tell inheriting to continue processing

        # when we 'stall' offline while on MQTT eventually retrigger HTTP
        # the reverse is not needed since we switch HTTP -> MQTT right-away
        # when HTTP fails (see async_http_request)
        if (self.curr_protocol is Protocol.MQTT) and (self.conf_protocol is Protocol.AUTO):
            self._switch_protocol(Protocol.HTTP)

        if (now - self.lastrequest) > self._retry_period:
            self._retry_period = self._retry_period + self.polling_period
            self.request(mc.NS_APPLIANCE_SYSTEM_ALL)

        return False