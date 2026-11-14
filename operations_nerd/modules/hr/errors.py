class HRError(ValueError):
    status = 422


class ValidationError(HRError):
    pass


class NotFound(HRError):
    status = 404


class Conflict(HRError):
    status = 409


class AuthorizationError(HRError):
    status = 403


class IdempotencyConflict(Conflict):
    pass
