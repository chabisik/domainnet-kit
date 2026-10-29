"""domainnet - a dependency-free framework to simulate and *show* a network of
independent domains that exchange messages and host their own services."""
from .automation import Churn, Scenario, TrafficGenerator
from .control import Controller
from .core import (
    CallTimeout,
    DeliveryError,
    Domain,
    DomainExists,
    DomainNetError,
    InvalidName,
    Link,
    LinkError,
    Message,
    Network,
    RemoteError,
    Service,
    ServiceContext,
    ServiceExists,
    UnknownDomain,
    UnknownService,
    UnknownTemplate,
    check_name,
    format_target,
    parse_target,
)
from .events import Event, EventBus
from .server import DashboardServer

__version__ = "0.1.0"

__all__ = [
    "Network", "Domain", "Service", "ServiceContext", "Message", "Link",
    "Controller", "DashboardServer", "TrafficGenerator", "Churn", "Scenario",
    "Event", "EventBus",
    "DomainNetError", "InvalidName", "DomainExists", "UnknownDomain",
    "ServiceExists", "UnknownService", "UnknownTemplate", "LinkError",
    "DeliveryError", "RemoteError", "CallTimeout",
    "check_name", "format_target", "parse_target",
]
