from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django_celery_beat.models import PeriodicTask
from instance.models import LiveChatRoom, Connector
from envelope.models import Message

class Command(BaseCommand):
    help = "Migrate chats from one connector to the other. This is useful for migrating a number between different connectors."

    def add_arguments(self, parser):
        parser.add_argument("from", nargs=1, type=int)
        parser.add_argument("to", nargs=1, type=int)

        # Named (optional) arguments
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Apply the migration",
        )

    def handle(self, *args, **options):
        print(options)
        # get from
        from_connector = int(options.get("from")[0])
        to_connector = int(options.get("to")[0])
        open_chats = LiveChatRoom.objects.filter(
            connector__id=from_connector,
            open=True
        )
        open_messages = Message.objects.filter(
            connector__id=from_connector,
            open=True
        )
        if open_chats.count():
            from_connector_object = open_chats.first().connector
            print("Migrating from connector: ", open_chats.first().connector)
            print(open_chats)
            print(open_chats.count(), "Chats found.")
            # get the to connector
            try:
                to_connector_object = Connector.objects.get(id=to_connector)
                print("Migrating to this connector: ", to_connector_object)
            except Connector.DoesNotExist:
                print("Error! Connector to migrate not found")
                to_connector_object = None
            # select and print all the findings
            # apply
            if options.get("apply"):
                print(f"Applying... migrating {open_chats.count()} open chats from {from_connector_object} to {to_connector_object}")
                update_rooms = open_chats.update(connector_id=to_connector_object.id)
                update_messages = open_messages.update(connector_id=to_connector_object.id)
                print("RESULT: ", update_rooms)
                print("RESULT: ", update_messages)

        else:
            print("No chats found on connector with id", from_connector)
