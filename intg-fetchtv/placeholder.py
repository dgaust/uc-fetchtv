"""
Artwork for channels that have no logo (every free-to-air channel, since Fetch
only serves logos for its subscription channels).

The Material "tv" icon on a dark surface, drawn at the size and aspect ratio of
Fetch's streaming-channel logos (266x150) so the two look alike in lists. It is
embedded as a data URI so the Remote needs no network access to show it.

:license: MPL-2.0, see LICENSE for more details.
"""

CHANNEL_PLACEHOLDER_PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAQoAAACWBAMAAADUNtt2AAAAGFBMVEXFyc69wca7v8SYm6A0NTghIiYgISQYGRx4"
    "bA+CAAAB7UlEQVR42u3asU4CQRAG4B0kSsPtHphQEQL2FvACEngWe1/EV/EJMLYk4gsYDwKtcrvEQptbOzlBL7OR"
    "ARN/Oi5TfMzOzh7ZoUv1Bz4lBQUUUEABBRRQQAEFFFBAAQUUUEDxtxXloOiUHxqLKdIOPzaJhRS+M2EngwZTI6NY"
    "JRPLj+47EYVvjkv8JbmvayOhWD2uPL8826MrfjKOeuxUnDxU3vjm9+Oyqgj0i2huQ0pZW4muRdOwTuReWgIKl+og"
    "Bc1EOrg3YcnwUh2cXJ8X+Oys3DmiiLmlxk4JKlQ2kzipQxW8s9IpvOVAAQUUUEABBRRQQAEFFFBAAQUUUEABBRRQ"
    "QAEFFFDsUkGsKCN6V+SjES9waSVvrLIJM2WGm7ZAhS85pbhDB94GXb/yFbp5p1XA0IFuOInqJB1WdMZK3PYvWhRy"
    "47k4F5kCiQbX7a9L/01JrndJz5LIHnnt31LR78sXTXp2MRPJBS27Li0a3spfO3dOU6EZpXg+fFovg77ZmMfQ9fUD"
    "6mZOqmvppFb7/FLdrvRcM0nISCkozq1CtXDALpbs4HHhpAmFnmJ4v9jBFMjPAyHZQRRZsvGgcQDF9qCQ2b+CFsOt"
    "5Lj95yJKdrZRf7MiMf6PQAEFFFBAAQUUUEABBRRQQAEFFP9H8QGPvWVz3eN+agAAAABJRU5ErkJggg=="
)

CHANNEL_PLACEHOLDER = "data:image/png;base64," + CHANNEL_PLACEHOLDER_PNG_BASE64
