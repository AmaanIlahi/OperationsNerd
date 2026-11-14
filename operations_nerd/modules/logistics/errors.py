"""Safe domain errors; no database credentials or SQL details are public."""

class LogisticsError(ValueError):
    """An expected command rejection."""

class ValidationError(LogisticsError):
    pass

RuleViolation = ValidationError

class NotFound(LogisticsError):
    pass

class Conflict(LogisticsError):
    pass

class IdempotencyConflict(Conflict):
    pass

class InsufficientStock(Conflict):
    pass

class AuthorizationError(LogisticsError):
    pass

class ConfigurationError(LogisticsError):
    pass
