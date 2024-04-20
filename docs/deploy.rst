DEPLOY / MIGRATION
======================================================================

COOKIE CUTTER
----------------------------------------------------------------------

Check this out: https://github.com/dudanogueira/rocketconnect_cookiecutter


Open Chat Connector Migration Command
----------------------------------------------------------------------

There is a new migration command that will help you migrating all open chats from connector X to connector Y

this is helpful if you want to change connectors. This will basically change in bulk the connector from the open chats of a given connector.


❯ docker compose -f local.yml run --rm django python manage.py migrate_connector_chat 6 2 --apply

this will apply the migration from open chats at connector id 6 to connector id 2