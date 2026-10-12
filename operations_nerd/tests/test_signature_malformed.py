import hashlib
import hmac
from insights.intake import verify_signature

def test_malformed_signatures_fail_closed():
    for signature in ['é'*64, 'a'*63, 'g'*64, 'sha256=é'*8, '']:
        assert verify_signature(b'body', signature, 'secret') is False

def test_valid_signature_forms_and_wrong_value():
    signature = hmac.new(b'secret', b'body', hashlib.sha256).hexdigest()
    for form in [signature, signature.upper(), 'sha256='+signature, ' '+signature+' ']:
        assert verify_signature(b'body', form, 'secret') is True
    assert verify_signature(b'other body', signature, 'secret') is False
