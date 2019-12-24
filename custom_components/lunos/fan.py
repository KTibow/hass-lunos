"""LUNOS Heat Recovery Ventilation Fan Control (e2/eGO)"""
# FIXME: can we subscribe to updates from the w1/w2 entities to avoid polling?

import time
import asyncio
import logging
import voluptuous as vol

from homeassistant.const import (
    CONF_NAME, CONF_ENTITY_ID,
    STATE_ON, STATE_OFF,
    SERVICE_TURN_ON, SERVICE_TURN_OFF, SERVICE_TOGGLE
)
from homeassistant.components.fan import (
    PLATFORM_SCHEMA,
    SUPPORT_SET_SPEED,
    SPEED_OFF, SPEED_LOW, SPEED_MEDIUM, SPEED_HIGH,
    FanEntity
)

from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_component import EntityComponent

from . import LUNOS_CODING_CONFIG

LOG = logging.getLogger(__name__)

LUNOS_DOMAIN = 'lunos'
DEFAULT_LUNOS_NAME = 'LUNOS Ventilation'

SPEED_TURBO = 'turbo' # FUTURE: support the special W2 extra-high mode
SPEED_LIST = [
    SPEED_OFF,
    SPEED_LOW,
    SPEED_MEDIUM,
    SPEED_HIGH
]
DEFAULT_SPEED = SPEED_MEDIUM

# configuration of switch states to active LUNOS speedsy
SPEED_SWITCH_STATES = {
    SPEED_OFF:    [ STATE_OFF, STATE_OFF ],
    SPEED_LOW:    [ STATE_ON,  STATE_OFF ],
    SPEED_MEDIUM: [ STATE_OFF, STATE_ON  ],
    SPEED_HIGH:   [ STATE_ON,  STATE_ON  ]
}

# delay all speed changes to > 3 seconds since the last relay switch change to avoid side effects
SPEED_CHANGE_DELAY_SECONDS = 4

ATTR_CFM = 'cfm' # note: even when off some LUNOS fans still circulate air
ATTR_CMHR = 'cmh'
ATTR_DB = 'dB'
ATTR_MODEL_NAME = 'model'
ATTR_CYCLE_SECONDS = 'cycle_seconds'
ATTR_VENTILATION_MODE = 'ventilation'  # [ normal, summer, exhaust-only ]
UNKNOWN = 'Unknown'

SERVICE_CLEAR_FILTER_REMINDER = 'lunos_clear_filter_reminder'
SERVICE_TURN_ON_SUMMER_VENTILATION = 'lunos_turn_on_summer_ventilation'
SERVICE_TURN_OFF_SUMMER_VENTILATION = 'lunos_turn_off_summer_ventilation'

CONF_CONTROLLER_CODING = 'controller_coding'
CONF_RELAY_W1 = 'relay_w1'
CONF_RELAY_W2 = 'relay_w2'
CONF_DEFAULT_SPEED = 'default_speed'
CONF_DEFAULT_FAN_COUNT = 'default_fan_count'
CONF_FAN_COUNT = 'fan_count'

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Optional(CONF_NAME): cv.string, # NOTE: we default the name later based on software
        vol.Optional(CONF_RELAY_W1): cv.string,  # cv.entity_id
        vol.Optional(CONF_RELAY_W2): cv.string,  # cv.entity_id
        vol.Optional(CONF_DEFAULT_SPEED, default=DEFAULT_SPEED): vol.In(SPEED_LIST),
#        vol.Optional(CONF_CONTROLLER_CODING, default='e2-usa'): cv.string,
        vol.Optional(CONF_CONTROLLER_CODING, default='e2-usa'): vol.In(LUNOS_CODING_CONFIG.keys()),
        vol.Optional(CONF_FAN_COUNT): vol.In( [ '1', '2', '3', '4' ]) # default is based on how controller is coded (see below)
    }
)

CFM_TO_CMH = 1.69901 # 1 cubic feet/minute = 1.69901 cubic meters/hour

# pylint: disable=unused-argument
async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """Initialize the LUNOS fans from config."""
    name = config.get(CONF_NAME)
    if not name:
        name = DEFAULT_LUNOS_NAME

    relay_w1 = config.get(CONF_RELAY_W1)
    relay_w2 = config.get(CONF_RELAY_W2)
    default_speed = config.get(CONF_DEFAULT_SPEED)

    LOG.info(f"Found LUNOS fan controller '{name}' configured with relays W1={relay_w1}, W2={relay_w2}'")

    fan = LUNOSFan(hass, config, name, relay_w1, relay_w2, default_speed)
    async_add_entities([fan], update_before_add=True)

    # expose service call APIs
    # FIXME: how are these tied to the specificy LUNOSFan instance?
    component = EntityComponent(LOG, LUNOS_DOMAIN, hass)
    component.async_register_entity_service(SERVICE_CLEAR_FILTER_REMINDER, {}, "async_clear_filter_reminder")
    component.async_register_entity_service(SERVICE_TURN_ON_SUMMER_VENTILATION, {}, "async_turn_on_summer_ventilation")
    component.async_register_entity_service(SERVICE_TURN_OFF_SUMMER_VENTILATION, {}, "async_turn_off_summer_ventilation")

    return True

class LUNOSFan(FanEntity):
    """Representation of a LUNOS fan."""

    def copy_model_config_attributes(self, model_config, attributes_list):
        

    def __init__(self, hass, conf, name, relay_w1_entity_id, relay_w2_entity_id, default_speed: str = DEFAULT_SPEED):
        """Init this sensor."""
        self._hass = hass
        self._name = name
        self._state = None
        self._w1_entity_id = relay_w1_entity_id
        self._w2_entity_id = relay_w2_entity_id
        self._default_speed = default_speed

        coding = conf.get(CONF_CONTROLLER_CODING)
        model_config = LUNOS_CODING_CONFIG[coding]

        # default fan count differs depending on what fans are attached to the controller (e2 = 2 fans, eGO = 1 fan)
        fan_count = conf.get(CONF_FAN_COUNT)
        if fan_count == None:
            fan_count = model_config[CONF_DEFAULT_FAN_COUNT]
        self._fan_count = fan_count

        self._state_attrs = {
            ATTR_MODEL_NAME: model_config['name'],
            CONF_CONTROLLER_CODING: coding,
            CONF_FAN_COUNT: fan_count,
            ATTR_VENTILATION_MODE: 'normal',  # TODO: support summer and exhaust-only
            ATTR_DB: UNKNOWN
        }

        for attribute in [ 'cycle_seconds',
                           'supports_summer_vent',
                           'supports_filter_reminder',
                           'turbo_mode',
                           'exhaust_only' ]: 
            if attribute in model_config:
                self._state_attrs[attribute] = model_config[attribute]

        # determine the current speed of the fans
        self.determine_current_speed_setting()
        self._last_state_change = time.time()

        super().__init__()
        LOG.info(f"Created LUNOS fan controller '{self._name}' (W1={relay_w1_entity_id}; W2={relay_w2_entity_id}; default_speed={default_speed})")

    # calculate the current CFM based on the current fan speed as well as the
    # number of fans configured by the user
    def update_attributes_based_on_mode(self):
        if self._state != None:
            coding = self._state_attrs[CONF_CONTROLLER_CODING]
            controller_config = LUNOS_CODING_CONFIG[coding]
            behavior = controller_config['behavior']

            cfm = None
            cmh = None
            if 'cfm' in behavior:
                cfm_for_mode = behavior['cfm'][self._state]
                fan_multiplier = self._fan_count / controller_config[CONF_DEFAULT_FAN_COUNT]
                cfm = cfm_for_mode * fan_multiplier
                cmh = cfm * CFM_TO_CMH
            elif 'chm' in behavior:
                chm_for_mode = behavior['chm'][self._state]
                fan_multiplier = self._fan_count / controller_config[CONF_DEFAULT_FAN_COUNT]
                cmh = chm_for_mode * fan_multiplier
                cfm = cmh / CFM_TO_CMH

            self._state_attrs[ATTR_CFM] = cfm
            self._state_attrs[ATTR_CMHR] = cmh

            if ATTR_DB in behavior:
                self._state_attrs[ATTR_DB] = controller_config[ATTR_DB][self._state]
            else:
                self._state_attrs[ATTR_DB] = UNKNOWN

            LOG.info(f"Updated '{self._name}' (speed={self._state}) attributes {self._state_attrs} based on controller config {controller_config}")

    @property
    def name(self):
        """Return the name of the fan."""
        return self._name

    @property
    def supported_features(self) -> int:
        """Flag supported features."""
        return SUPPORT_SET_SPEED

    @property
    def speed_list(self) -> list:
        """Get the list of available speeds."""
        return SPEED_LIST

    @property
    def speed(self) -> str:
        """Return the current speed."""
        return self._state

    @property
    def is_on(self) -> bool:
        """Return true if entity is on."""
        if self._state is None:
            return False
        # NOTE: for 4-speed fans, there is never a true "OFF" setting
        return self._state != SPEED_OFF

    # TOOD: last updated? controller type? cfm?
    @property
    def device_state_attributes(self):
        """Return state attributes."""
        return self._state_attrs

    async def _async_set_state(self, speed):
        """Handle state update from fan."""
        LOG.info(f"async changing LUNOS fan '{self._name}' to speed '{self._state}'")
        self._state = speed
        self.async_schedule_update_ha_state()

     # probe the two relays to determine current state and find the matching speed switch state
    def determine_current_speed_setting(self):
        w1 = self._hass.states.get(self._w1_entity_id)
        if not w1:
            LOG.warning(f"LUNOS could not find W1 entity {self._w1_entity_id}, cannot determine fan speed.")
            return

        w2 = self._hass.states.get(self._w2_entity_id)
        if not w2:
            LOG.warning(f"LUNOS could not find W2 entity {self._w2_entity_id}, cannot determine fan speed.")
            return

        # determine the current speed
        current_state = [ w1.state, w2.state ]
        for current_speed, speed_state in SPEED_SWITCH_STATES.items():
            if current_state == speed_state:
               break
 
        # update the speed state, if a change has been detected
        if current_speed != self._state:
            LOG.info(f"Detected LUNOS speed for '{self._name}' = {current_speed} ({self._w1_entity_id}={w1.state}, {self._w2_entity_id}={w2.state})")
            self._state = current_speed
            self.update_attributes_based_on_mode()

        return current_speed

    async def async_set_speed(self, speed: str) -> None:
        """Set the speed of the fan."""
        switch_states = SPEED_SWITCH_STATES[speed]
        if switch_states == None:
            LOG.error(f"LUNOS fan '{self._name}' DOES NOT support speed '{speed}'; ignoring request to change speed.")
            return

        # ignore if the fan is already set to this speed
        if speed == self._state:
            return

        # flipping W1 or W2 within 3 seconds instructs the LUNOS controller to either clear the
        # filter warning light (W1) or turn on the summer/night ventilation mode (W2), thus
        # delay all state changes to be > 3 seconds since the last switch change
        time_passed = time.time() - self._last_state_change
        if time_passed < SPEED_CHANGE_DELAY_SECONDS:
            delay = max(0, SPEED_CHANGE_DELAY_SECONDS - time_passed)
            LOG.error(f"To avoid LUNOS controller confusion, speed changes must >= {SPEED_CHANGE_DELAY_SECONDS} seconds apart; sleeping {delay} seconds")
            await asyncio.sleep(delay)

        self.set_relay_switch_state(self._w1_entity_id, switch_states[0])
        self.set_relay_switch_state(self._w2_entity_id, switch_states[1])

        # update the state and inform Home Assistant that it has changed
        self._state = speed
        LOG.info(f"Changed LUNOS fan '{self._name}' to speed '{self._state}'")
        self.async_schedule_update_ha_state()
        self.update_attributes_based_on_mode()

    async def async_turn_on(self, speed: str = None, **kwargs) -> None:
        """Turn the fan on."""
        # TODO: should this turn on to the default speed, or the last speed before turning off?
        if speed is None:
            speed = self._default_speed

        await self.async_set_speed(speed)

    async def async_turn_off(self, **kwargs) -> None:
        """Turn the fan off."""
        await self.async_set_speed(SPEED_OFF)

    def switch_service_call(self, method, relay_entity_id):
        LOG.info(f"Calling switch {method} for {relay_entity_id}")
        self._hass.services.call('switch', method, { 'entity_id': relay_entity_id }, False)
        self._last_state_change = time.time()

    def set_relay_switch_state(self, relay_entity_id, state):
        LOG.info(f"Setting relay '{relay_entity_id}' to {state}")
        if state == STATE_OFF:
            self.switch_service_call('turn_off', relay_entity_id)
        else:
            self.switch_service_call('turn_on', relay_entity_id)

    def toggle_relay_to_set_lunos_mode(self, entity_id):
        save_speed = self._state
        for i in range(3):
            self.switch_service_call(SERVICE_TURN_OFF, entity_id)
            self.switch_service_call(SERVICE_TURN_ON, entity_id)

        # reset back to the speed prior to toggling relay
        self.async_set_speed(save_speed)

    async def async_update(self):
        """Attempt to retrieve current state of the fan by inspecting the switch state."""
        self.determine_current_speed_setting()

    # flipping W1 within 3 seconds instructs the LUNOS controller to clear the filter warning light
    async def async_clear_filter_reminder(self):
        LOG.info(f"Clearing the change filter reminder light for LUNOS controller '{self._name}'")
        self.toggle_relay_to_set_lunos_mode(self._w1_entity_id)

    def supports_summer_ventilation(self):
        coding = self._state_attrs[CONF_CONTROLLER_CODING]
        controller_config = LUNOS_CODING_CONFIG[coding]
        return controller_config['supports_summer_vent'] == True

    # flipping W2 within 3 seconds instructs the LUNOS controller to turn on summer ventilation mode
    async def async_turn_on_summer_ventilation(self):
        if not self.supports_summer_ventilation():
            LOG.info(f"LUNOS controller '{self._name}' is coded and DOES NOT support summer ventilation")
            return

        LOG.info(f"Turning summer ventilation mode ON for LUNOS controller '{self._name}'")
        self.toggle_relay_to_set_lunos_mode(self._w2_entity_id)

    async def async_turn_off_summer_ventilation(self):
        if not self.supports_summer_ventilation():
            return # silently ignore as it is already off

        # must wait 10 seconds since the last time W2 was flipped before turning off ventilation will work
        required_delay = 10
        time_passed = time.time() - self._last_state_change
        if time_passed < required_delay:
            delay = max(0, required_delay - time_passed)
            LOG.error(f"To avoid LUNOS controller confusion, summer ventilation changes >= {required_delay} seconds since last relay switch; sleeping {delay} seconds")
            await asyncio.sleep(delay)

        LOG.info(f"Turning summer ventilation mode OFF for LUNOS controller '{self._name}'")

        # toggle the switch back and forth once (thus restoring existing state) to clear summer ventilation mode
        self.switch_service_call(SERVICE_TOGGLE, self._w2_entity_id)
        self.switch_service_call(SERVICE_TOGGLE, self._w2_entity_id)