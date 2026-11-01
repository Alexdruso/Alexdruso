"""poolbot -- snipe Clubsportive pool slots the moment they are released.

The Clubsportive app (``com.mysports.branded.clubsportive``) is a white-label build of the
MySports / Sport Alliance (Magicline) platform. There is no web booking portal, so this
package talks to the same HTTP API the app uses. The API surface is not public: it is
described in ``endpoints.toml``, which ``poolbot.extract_apk`` generates from the APK.
"""

__version__ = "1.0.0"
