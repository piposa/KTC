# KTC - Klipper Tool Changer code (v.2)
# Base classes and types for KTC to inherit from.
# This file is part of the KTC extension for the Klipper firmware.
# This should not import any other KTC module but can be imported by any KTC module.
#
# Copyright (C) 2024 Andrei Ignat <andrei@ignat.se>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
from __future__ import annotations
import ast, typing, re
import cProfile, pstats, io
from enum import IntEnum, Enum
from .ktc_heater import (   # pylint: disable=relative-beyond-top-level
    KtcToolExtruder,
    DEFAULT_HEATER_ACTIVE_TO_STANDBY_DELAY,
    DEFAULT_HEATER_STANDBY_TO_POWERDOWN_DELAY,
)


# Only import these modules in Dev environment. Consult Dev_doc.md for more info.
if typing.TYPE_CHECKING:
    from ...klipper.klippy import configfile, gcode, klippy
    from ...klipper.klippy.extras import gcode_macro as klippy_gcode_macro
    from . import ktc_log, ktc_toolchanger, ktc_tool, ktc, ktc_persisting

# Value of Unknown and None tools.
TOOL_NUMBERLESS_N = -3
TOOL_UNKNOWN_N = -2
TOOL_NONE_N = -1

# Parameters available for inheritance by all tools and their default values.
PARAMS_TO_INHERIT = {"_engage_gcode": "",
                     "_disengage_gcode": "",
                     "_init_gcode": "",
                     "_tool_select_gcode": "",
                     "_tool_deselect_gcode": "",
                     "force_deselect_when_parent_deselects": True,
                     "parent_must_be_selected_on_deselect": True,
                     "_heaters_config": "",
                     "fans": "",
                     "offset": [0.0, 0.0, 0.0],
                     "requires_axis_homed": "XYZ",
                     "_heater_active_to_standby_delay_in_config":
                         DEFAULT_HEATER_ACTIVE_TO_STANDBY_DELAY,
                     "_heater_standby_to_powerdown_delay_in_config":
                         DEFAULT_HEATER_STANDBY_TO_POWERDOWN_DELAY,
                     }

class KtcConfigurableEnum(Enum):
    @classmethod
    def get_value_from_configuration(cls, config: 'configfile.ConfigWrapper', value_name: str,
                                     default_name: typing.Optional[str] = None):
        val = typing.cast(str, config.get(value_name, default_name))  # type: ignore
        val = val.strip().upper()
        if val == "":
            raise ValueError(f"Value {value_name} not found in configuration.")
        if val not in cls.list_valid_values():
            raise ValueError(f"Value {val} not valid for {value_name}"
                                +f" in configuration for {config.get_name()}."
                + f"Valid values are: {cls.list_valid_values()}")
        return cls[val]

    @classmethod
    def list_valid_values(cls):
        return [str(name) for name in cls.__members__]

    def __str__(self):
        return f"'{self.name}'"

class KtcBaseClass:
    """Base class for KTC. Contains common methods and properties."""
    def __init__(self, config: "configfile.ConfigWrapper"): # type: ignore
        self.config = typing.cast('configfile.ConfigWrapper', config)
        self.name: str = ""

        #: To store performance data on startup for later logging.
        self.debug_init_profile:str = None    # type: ignore

        self.debug_with_profile: bool = False

        # Can contain "X", "Y", "Z" or a combination.
        self.requires_axis_homed: str = ""
        self._state = self.StateType.NOT_CONFIGURED

        self.force_deselect_when_parent_deselects: bool = None  # type: ignore
        self.parent_must_be_selected_on_deselect: bool = None  # type: ignore

        # If this is a empty object then don't load the config.
        if config is None:
            return

        self.force_deselect_when_parent_deselects: bool = config.getboolean(
            "force_deselect_when_parent_deselects", None)  # type: ignore

        self.parent_must_be_selected_on_deselect: bool = config.getboolean(
            "parent_must_be_selected_on_deselect", None)  # type: ignore

        self.printer : 'klippy.Printer' = config.get_printer()
        self.reactor: 'klippy.reactor.Reactor' = self.printer.get_reactor()
        self.gcode = typing.cast('gcode.GCodeDispatch', self.printer.lookup_object("gcode"))
        self.log: 'ktc_log.KtcLog' = None # type: ignore # We are loading it later.
        self._ktc: 'ktc.Ktc' = None # type: ignore # We are loading it later.

        self._state = self.StateType.NOT_CONFIGURED
        self.offset: list[float, float, float] = None   # type: ignore

        self.params = self.get_params_dict_from_config(config)
        # Get inheritable parameters from the config.
        # Empty strings are NOT overwritten by the parent object in configure_inherited_params.
        # Must be set to None as standard.
        # Initalized to default values in KTC as topmost parent.
        self._engage_gcode = config.get("engage_gcode", None)  # type: ignore
        self._disengage_gcode = config.get("disengage_gcode", None)  # type: ignore
        self._init_gcode = config.get("init_gcode", None)  # type: ignore
        self._tool_select_gcode = config.get("tool_select_gcode", None)     # type: ignore
        self._tool_deselect_gcode = config.get("tool_deselect_gcode", None) # type: ignore

        self._heaters_config: str = self.config.get("heater", None)    # type: ignore

        # Minimum time is 0.1 seconds. 0 disables the timer thus never changes the temperature.
        self._heater_active_to_standby_delay_in_config = self.config.getfloat(
            "heater_active_to_standby_delay", None, 0.1)    # type: ignore
        self._heater_standby_to_powerdown_delay_in_config = self.config.getfloat(
            "heater_standby_to_powerdown_delay", None, 0.1) # type: ignore

        # Fans are a list of lists with the first value being the name
        # of the fan and the second value being the speed scaling 0-1.
        f = typing.cast(str, self.config.get("fans", "")).replace(" ", "")
        self.fans = [x.split(":") for x in f.split(",")] if f != "" else []
        fan: list[typing.Any]
        for fan in self.fans:
            errmsg = ("Invalid fan speed scaling for" +
                      f" {self.config.get_name()}: {fan[0]}. " +
                      "Fan speed must be a float between 0 and 1.")
            if len(fan) == 1:
                fan.append(1.0)
            elif not self.is_float(fan[1]):
                raise config.error(errmsg)
            else:
                if fan[1] is not None:
                    fan[1] = fan[1]
            if fan[1] < 0 or fan[1] > 1:
                raise config.error(errmsg)
            if len(fan) != 2:
                raise config.error(f"Fan settings for {self.config.get_name()} are invalid.")

        # requires_axis_homed can contain "X", "Y", "Z" or a combination. Remove all other.
        self.requires_axis_homed: str = self.config.get(
            "requires_axis_homed", None)   # type: ignore
        if self.requires_axis_homed is not None and self.requires_axis_homed != "":
            self.requires_axis_homed = re.sub(r'[^XYZ]', '', self.requires_axis_homed.upper())

        # Initiating values are only red once and then saved to the persistent state and
        # must be removed from the config file to continue.
        self._initiating_config = {}
        # Offset as a list of 3 floats. Also valid for global_offset.
        init: str = ""
        # Check first if the section exists in the configuration, get_prefix_options will fail otherwise.
        if config.has_section(config.get_name()):
            for init in config.get_prefix_options("init_"):
                init = init.strip().lower()
                if 'offset' in init:
                    try:
                        if init == "init_global_offset" and self.__class__.__name__ != "Ktc":
                            raise ValueError(
                                "init_global_offset is only valid for the topmost KTC object.")
                        elif init != "init_global_offset" or init == "init_offset":
                            raise ValueError(
                                f"Invalid initializing option name {init} "
                                + "for {self.config.get_name()}.")
                        v = typing.cast(str, config.get(init)).replace(" ", "")
                        if v:
                            vl = [float(x) for x in v.split(",")]
                            if len(vl) != 3:
                                raise ValueError(f"{init} must be a list of 3 floats.")
                            self._initiating_config[init.lstrip("init_")] = vl
                    except Exception as e:
                        raise self.config.error(f"Invalid {init} for {self.config.get_name()}: {e}")

    def configure_inherited_params(self):
        '''Load inherited parameters from instances that this instance inherits from.
        This is called after all instances are loaded.'''
        # Ref. to the ktc_persisting object. Loaded by ktc_log.
        self._ktc_persistent: 'ktc_persisting.KtcPersisting' = (  # type: ignore # pylint: disable=attribute-defined-outside-init
            self.printer.lookup_object("ktc_persisting")
        )

        # Check if any initiating values are set.
        # offset and global_offset are valid.
        if len(self._initiating_config) > 0:
            for key in (key for key in self._initiating_config if "offset" in key):
                self.persistent_state_set(key, self._initiating_config[key])
                raise self.config.error(
                    f"{key} for {self.config.get_name()} successfully saved as"
                    + f" {self._initiating_config[key]}. "
                    + "Remove the setting from config and restart Klipper to continue.")

        # Check for circular inheritance.
        if self.state >= self.StateType.CONFIGURED:
            return
        elif self.state == self.StateType.CONFIGURING:
            raise ValueError("Can't configure inherited parameters while already configuring "
                             + self.config.get_name())
        # Ref. to ktc objects.
        self._ktc = typing.cast('ktc.Ktc', self.printer.lookup_object("ktc"))
        self.log = typing.cast('ktc_log.KtcLog', self.printer.lookup_object(
            "ktc_log"))  # Load the log object.

        if self.debug_init_profile is not None:
            self.log.trace("KTC startup profile: " + str(self.debug_init_profile))

        self.state = self.StateType.CONFIGURING

        # Get Offset from persistent storage
        self.offset = self.persistent_state.get("offset", None)

        #  Set the parent object
        if isinstance(self, KtcBaseToolClass):
            parent = self.toolchanger
        elif isinstance(self, KtcBaseChangerClass):
            parent = self.parent_tool
            if parent is None:
                parent = typing.cast('ktc.Ktc', self.printer.lookup_object("ktc"))
        elif isinstance(self, KtcBaseClass):
            parent = self
        else:
            raise ValueError("Can't configure inherited parameters for object: " + str(type(self)))

        if self != parent:
            # Set the parameters from the parent object if they are not set.
            for attr, _ in PARAMS_TO_INHERIT.items():
                if getattr(self, attr) is None:
                    setattr(self, attr, getattr(parent, attr))
        else:
            # For top ktc object initialize unused parameters.
            for attr, default_value in PARAMS_TO_INHERIT.items():
                if getattr(self, attr) is None:
                    setattr(self, attr, default_value)

            for v in parent.params: # type: ignore
                if v not in self.params:
                    self.params[v] = parent.params[v]   # type: ignore

    @staticmethod
    def get_params_dict_from_config(config: 'configfile.ConfigWrapper'):
        """Get a dict of atributes starting with params_ from the config."""
        result = {}

        if config is None or not config.has_section(config.get_name()):
            return result

        # Get all options that start with "params_" and add them to the result dict.
        for option in config.get_prefix_options("params_"):
            try:
                value : str = config.get(option)
                # Boolean:
                if value.lower().strip() in ("true", "false"):
                    result[option] = config.getboolean(option)
                # Integer:
                elif value.replace("-", "").replace(" ", "").isdigit():
                    result[option] = config.getint(option)
                # Float:
                elif value.replace(".", "").replace("-", "").replace(" ", "").isdigit():
                    result[option] = config.getfloat(option)
                # List of Integers:
                elif value.replace("-", "").replace(" ", "").replace(",", "").isdigit():
                    result[option] = [int(x) for x in value.split(",")]
                # List of Floats:
                elif value.replace(".", "").replace("-", "").replace(
                    " ", "").replace(",", "").isdigit():
                    result[option] = [float(x) for x in value.split(",")]
                # String with quotes:
                elif value.startswith('"') and value.endswith('"'):
                    result[option] = ast.literal_eval(value)
                # String with single quotes:
                elif value.startswith("'") and value.endswith("'"):
                    result[option] = ast.literal_eval(value)
                # Check if it is a valid String:
                else:
                    result[option] = ast.literal_eval('"' + value + '"')
            except ValueError as e:
                raise config.error(
                    "Option '%s' in section '%s' is not a valid literal: %s."
                    % (option, config.get_name(), e)
                )
        return result

    class StateType(IntEnum, KtcConfigurableEnum):
        """Constants for the status of the toolchanger.
        Using dataclasses to allow for easy traversal of the values."""
        ERROR= -50              # Toolchanger or tool is in error state.
        NOT_CONFIGURED = -12    # Toolchanger or tool is not configured.
        CONFIGURING = -11       # Toolchanger or tool is configuring.
        CONFIGURED = -10        # Toolchanger or tool is configured but not initialized.
        UNINITIALIZED = -2      # Toolchanger or tool is uninitialized.
        INITIALIZING = -1       # Toolchanger or tool is initializing.
        INITIALIZED = 0         # Toolchanger or tool is initialized but not ready.
        READY = 1               # Toolchanger or tool is ready to be used.
        CHANGING = 2            # Toolchanger or tool is changing tool.
        ENGAGING = 3            # Toolchanger is engaging.
        SELECTING = 3           # Tool is selecting.
        DISENGAGING = 4         # Toolchanger or tool is disengaging.
        DESELECTING = 4         # Tool is deselecting.
        ENGAGED = 5             # Tollchanger or tool is engaged.
        SELECTED = 5            # Tool is selected.
        ACTIVE = 10             # Tool is active as main engaged tool for ktc.

        @classmethod
        def list_valid_values(cls):
            return [name for name, _ in cls.__members__]

        def __str__(self):
            return f'{self.name}'

    @property
    def state(self):
        return self._state
    @state.setter
    def state(self, value):
        try:
            self._state = self.StateType[str(value).upper()]
        except KeyError as e:
            raise ValueError("Invalid state value: " + str(value)) from e


    @property
    def persistent_state(self) -> dict:
        '''Return the persistent state from file.
        Is initialized inside _handle_connect.
        Use persistent_state_set to set the state.'''
        c = self._get_type_for_persistent_state()
        v: dict = self._ktc_persistent.content.get("State", {})
        return v.get(c, {})

    def persistent_state_set(self, key: str, value: typing.Any):
        '''Set the persistent state for the object. Use persistent_state to get the state.'''
        c = self._get_type_for_persistent_state()

        state: dict = self._ktc_persistent.content.get("State", {}).get(c, {})
        state[key] = value

        self._ktc_persistent.save_variable(c, str(state), "State", True)

    def _get_type_for_persistent_state(self) -> str:
        if self._ktc_persistent is None:
            self._ktc_persistent: 'ktc_persisting.KtcPersisting' = (  # type: ignore # pylint: disable=attribute-defined-outside-init
                self.printer.lookup_object("ktc_persisting")
            )
        if isinstance(self, KtcBaseToolClass):
            return "ktc_tool_" + self.name.lower()
        elif isinstance(self, KtcBaseChangerClass):
            return "ktc_toolchanger_" + self.name.lower()
        elif isinstance(self, KtcBaseClass):
            return "ktc"
        else:
            raise ValueError(f"Can't get persistent state for object: {type(self)}")

    @staticmethod
    def is_float(value: str) -> bool:
        try:
            float(value)
            return True
        except ValueError:
            return False

    @staticmethod
    def parse_bool(value: str) -> bool:
        if value.isnumeric():
            return bool(int(value))
        return value.strip().lower() in ("true", "1", "yes")

    def run_with_profile(self, method, *args, **kwargs):
        '''Run a profile on a method. Used for debugging.'''
        if not self.run_with_profile:
            method(*args, **kwargs)
            return
        pr = cProfile.Profile()
        pr.enable()
        method(*args, **kwargs)
        pr.disable()
        s = io.StringIO()
        ps = pstats.Stats(pr, stream=s).sort_stats('cumulative')
        ps.print_stats()
        stats_string = s.getvalue()
        s.close()

        # Trim to 20 lines.
        carriage_return_count = stats_string.count('\n')
        if carriage_return_count >= 20:
            index = -1
            for _ in range(20):
                index = stats_string.find('\n', index + 1)
            # index now contains the index of the 10th carriage return
        else:
            index = len(stats_string) - 1

        self.log.trace(
            f"Performance profile for {method.__name__}:\n" + stats_string[:index])

class KtcBaseChangerClass(KtcBaseClass):
    '''Base class for toolchangers. Contains common methods and properties.'''
    def __init__(self, config: 'configfile.ConfigWrapper'):
        super().__init__(config)
        self.name: str = str(config.get_name()).split(" ", 1)[1]
        # The parent tool of the toolchanger if it is not default changer.
        self.parent_tool: 'ktc_tool.KtcTool' = None # type: ignore
        # self.selected_tool = KtcConstantsClass.TOOL_NONE
        self.tools: dict[str, 'ktc_tool.KtcTool'] = {}
        self._engage_gcode_template: klippy_gcode_macro.GCodeMacro = None # type: ignore
        self._disengage_gcode_template: klippy_gcode_macro.GCodeMacro = None # type: ignore

class KtcBaseToolClass(KtcBaseClass):
    '''Base class for tools. Contains common methods and properties.'''
    def __init__(self, config: "configfile.ConfigWrapper",
                 name: str = "", number: int = TOOL_NUMBERLESS_N):
        super().__init__(config)

        self.name = name        # Override the name in case it is supplied.
        self.number = number
        # Is overridden by the tool object.
        self._toolchanger: 'ktc_toolchanger.KtcToolchanger' = None   # type: ignore
        self.toolchanger: 'ktc_toolchanger.KtcToolchanger' = self._toolchanger # type: ignore
        self.extruder = KtcToolExtruder(self)

    @KtcBaseClass.state.setter
    def state(self, value):
        '''Set state of the tool. If the tool is selected or active then the toolchanger
        is also set to the same state.'''
        super(KtcBaseToolClass, type(self)).state.fset(self, value) # type: ignore

        # TOOL_UNKNOWN and TOOL_NONE has no _ktc object.
        if self in KtcConstantsClass.INVALID_TOOLS:
            return

        if self._ktc.propagate_state:
            self.toolchanger.state = value

        if value == self.StateType.SELECTED and self._ktc.propagate_state:
            self.toolchanger.selected_tool = self   # type: ignore # Child class
        elif value == self.StateType.ACTIVE and self._ktc.propagate_state:
            self.toolchanger.selected_tool = self   # type: ignore # Child class
            self._ktc.active_tool = self

        if value == self.StateType.ERROR:
            self.log.always("KTC Tool %s is now in error state." % self.name)

    def select(self, final_selected=False):
        pass

    def deselect(self):
        pass

class KtcConstantsClass:
    '''Constants for KTC. These are to be inherited by other classes.
    '''
    # Value of Unknown and None tools are set in module scope.
    TOOL_NUMBERLESS_N = TOOL_NUMBERLESS_N
    TOOL_UNKNOWN_N = TOOL_UNKNOWN_N
    TOOL_NONE_N = TOOL_NONE_N
    TOOL_UNKNOWN = typing.cast(
        'ktc_tool.KtcTool',
        KtcBaseToolClass(name="tool_unknown",
                         number=TOOL_UNKNOWN_N,
                         config = None))   # type: ignore
    TOOL_NONE = typing.cast(
        'ktc_tool.KtcTool',
        KtcBaseToolClass(name="tool_none",
                         number=TOOL_NONE_N,
                         config = None))         # type: ignore
    TOOL_NONE._state = TOOL_UNKNOWN._state = KtcBaseClass.StateType.CONFIGURED  # pylint: disable=protected-access
    INVALID_TOOLS = (TOOL_UNKNOWN, TOOL_NONE, None)
