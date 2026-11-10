BlueShield Global — consolidated replacement package

This release is built from the uploaded blueshield.zip backend and the latest
uploaded dashboard. Backend modules are placed under backend/ and the dashboard
under web/ to match the imports and paths used by the application.

Before replacing anything, back up your existing project. Copy/merge backend/
and web/ into the project root. Preserve your existing backend/blueshield.db,
project-root .env, and backend/data/ files (especially any downloaded ZoneGuard
GeoJSON/checkpoint data). Do not overwrite those local runtime files.

See INSTALL.txt for the Windows startup and verification steps.
