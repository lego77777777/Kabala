from .api_jwk import PyJWK, PyJWKSet
from .api_jws import (
    PyJWS,
    get_algorithm_by_name,
    get_unverified_header,
    register_algorithm,
    unregister_algorithm,
)
from .api_jwt import PyJWT, decode, decode_complete, encode
from .exceptions import (
    DecodeError,
    ExpiredSignatureError,
    ImmatureSignatureError,
    InvalidAlgorithmError,
    InvalidAudienceError,
    InvalidIssuedAtError,
    InvalidIssuerError,
    InvalidKeyError,
    InvalidSignatureError,
    InvalidTokenError,
    MissingRequiredClaimError,
    PyJWKClientConnectionError,
    PyJWKClientError,
    PyJWKError,
    PyJWKSetError,
    PyJWTError,
)
# Kabala: jwks_client (downloads keys over the network) is not shipped; Kabala's keys are configured, never fetched
from .warnings import InsecureKeyLengthWarning

__version__ = "2.15.1"

__title__ = "PyJWT"
__description__ = "JSON Web Token implementation in Python"
__url__ = "https://pyjwt.readthedocs.io"
__uri__ = __url__
__doc__ = f"{__description__} <{__uri__}>"

__author__ = "José Padilla"
__email__ = "hello@jpadilla.com"

__license__ = "MIT"
__copyright__ = "Copyright 2015-2026 José Padilla"


__all__ = [
    "PyJWS",
    "PyJWT",
    "PyJWK",
    "PyJWKSet",
    "decode",
    "decode_complete",
    "encode",
    "get_unverified_header",
    "register_algorithm",
    "unregister_algorithm",
    "get_algorithm_by_name",
    # Warnings
    "InsecureKeyLengthWarning",
    # Exceptions
    "DecodeError",
    "ExpiredSignatureError",
    "ImmatureSignatureError",
    "InvalidAlgorithmError",
    "InvalidAudienceError",
    "InvalidIssuedAtError",
    "InvalidIssuerError",
    "InvalidKeyError",
    "InvalidSignatureError",
    "InvalidTokenError",
    "MissingRequiredClaimError",
    "PyJWKClientConnectionError",
    "PyJWKClientError",
    "PyJWKError",
    "PyJWKSetError",
    "PyJWTError",
]
