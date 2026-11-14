class AccountingError(ValueError):
    status = 422


class Conflict(AccountingError):
    status = 409


class NotFound(AccountingError):
    status = 404


class Forbidden(AccountingError):
    status = 403
