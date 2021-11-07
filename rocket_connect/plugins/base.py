import base64
import json
import logging
import mimetypes
import random
import string
import tempfile
import time
from io import BytesIO

import qrcode
import requests
import zbarlight
from django import forms
from django.conf import settings
from django.db import IntegrityError
from django.http import JsonResponse
from django.template import Context, Template
from envelope.models import LiveChatRoom
from PIL import Image

from emojipy import emojipy


class Connector(object):
    def __init__(self, connector, message, type, request=None):
        self.connector = connector
        self.type = type
        self.config = self.connector.config
        # get timezone
        self.timezone = (
            self.config.get("timezone") or settings.TIME_ZONE or "America/Sao_Paulo"
        )
        # self.message must be a dictionary
        if message:
            self.message = json.loads(message)
        else:
            self.message = None
        self.request = request
        self.message_object = None
        self.rocket = None
        self.room = None
        self.logger = logging.getLogger("teste")

    def status_session(self):
        return True

    def close_session(self):
        return True

    def logger_info(self, message):
        output = "{0} > {1} > {2}".format(self.connector, self.type.upper(), message)
        if self.get_message_id():
            output = "MESSAGE ID {0} > ".format(self.get_message_id()) + output
        self.logger.info(output)

    def logger_error(self, message):
        self.logger.error(
            "{0} > {1} > {2}".format(self.connector, self.type.upper(), message)
        )

    def incoming(self):
        """
        this method will process the incoming messages
        and ajust what necessary, to output to rocketchat
        """
        self.logger_info("INCOMING MESSAGE: {0}".format(self.message))
        return JsonResponse(
            {
                "connector": self.connector.name,
            }
        )

    def outcome_qrbase64(self, qrbase64):
        """
        this method will send the qrbase64 image to the connector managers at RocketChat
        """
        # send message as bot
        rocket = self.get_rocket_client(bot=True)
        # create im for managers
        managers = self.connector.get_managers()
        if settings.DEBUG:
            print("GOT MANAGERS: ", managers)
        im_room = rocket.im_create(username="", usernames=managers)
        im_room_created = im_room.json()

        # send qrcode
        try:
            data = qrbase64.split(",")[1]
        except IndexError:
            data = qrbase64
        imgdata = base64.b64decode(str.encode(data))
        with tempfile.NamedTemporaryFile(suffix=".png") as tmp:
            tmp.write(imgdata)
            if im_room_created["success"]:
                rocket.rooms_upload(
                    rid=im_room_created["room"]["rid"],
                    file=tmp.name,
                    msg=":rocket: Connect > *Connector Name*: {0}".format(
                        self.connector.name
                    ),
                    description="Scan this QR Code at your Whatsapp Phone:",
                )
            # out come qr to room
            managers_channel = self.connector.get_managers_channel(as_string=False)
            for channel in managers_channel:
                # get room id
                room_infos = rocket.rooms_info(room_name=channel.replace("#", ""))
                if room_infos.ok:
                    rid = room_infos.json().get("room", {}).get("_id", None)
                    if rid:
                        send_qr_code = rocket.rooms_upload(
                            rid=rid,
                            file=tmp.name,
                            msg=":rocket: Connect > *Connector Name*: {0}".format(
                                self.connector.name
                            ),
                            description="Scan this QR Code at your Whatsapp Phone:",
                        )
                        self.logger_info(
                            "SENDING QRCODE TO ROOM... {0}: {1}".format(
                                channel, send_qr_code.json()
                            )
                        )
                else:
                    self.logger_error(
                        "FAILED TO SEND QRCODE TO ROOM... {0}: {1}".format(
                            channel, room_infos.json()
                        )
                    )

    def outcome_file(self, base64_data, room_id, mime, filename=None, description=None):
        if settings.DEBUG:
            print("OUTCOMING FILE TO ROCKETCHAT")
        # prepare payload
        filedata = base64.b64decode(base64_data)
        extension = mimetypes.guess_extension(mime)
        if not filename:
            # random filename
            filename = "".join(
                random.choices(string.ascii_letters + string.digits, k=16)
            )
        # write file to temp file
        # TODO: maybe dont touch the hard drive, keep it buffer
        with tempfile.NamedTemporaryFile(suffix=extension) as tmp:
            tmp.write(filedata)
            headers = {"x-visitor-token": self.get_visitor_token()}
            # TODO: open an issue to be able to change the ID of the uploaded file like a message allows
            files = {"file": (filename, open(tmp.name, "rb"), mime)}
            data = {}
            if description:
                data["description"] = description
            url = "{0}/api/v1/livechat/upload/{1}".format(
                self.connector.server.url, room_id
            )
            deliver = requests.post(url, headers=headers, files=files, data=data)
            self.logger_info("RESPONSE OF FILE OUTCOME: {0}".format(deliver.json()))
            timestamp = int(time.time())
            self.message_object.payload[timestamp] = {
                "data": "sent attached file to rocketchat"
            }
            if deliver.ok:
                if settings.DEBUG and deliver.ok:
                    print("teste, ", deliver)
                    print("OUTCOME FILE RESPONSE: ", deliver.json())
                self.message_object.response[timestamp] = deliver.json()
                self.message_object.delivered = deliver.ok
                self.message_object.save()

            if self.connector.config.get(
                "outcome_attachment_description_as_new_message", True
            ):
                if description:
                    self.outcome_text(room_id, description)

            return deliver

    def outcome_text(self, room_id, text, message_id=None):
        deliver = self.room_send_text(room_id, text, message_id)
        timestamp = int(time.time())
        self.message_object.payload[timestamp] = json.loads(deliver.request.body)
        self.message_object.response[timestamp] = deliver.json()
        if settings.DEBUG:
            self.logger_info("DELIVERING... {0}".format(deliver.request.body))
            self.logger_info("RESPONSE... {0}".format(deliver.json()))
        if deliver.ok:
            if settings.DEBUG:
                self.logger_info(
                    "MESSAGE DELIVERED... {0}".format(self.message_object.id)
                )
            self.message_object.delivered = True
            self.message_object.room = self.room
            self.message_object.save()
            return deliver
        else:
            self.logger_info(
                "MESSAGE *NOT* DELIVERED... {0}".format(self.message_object.id)
            )
            # save payload and save message object
            self.message_object.save()
            # room can be closed on RC and open here
            r = deliver.json()
            # TODO: when sending a message already sent, rocket doesnt return a identifiable message
            # file a bug, and test it more
            if r.get("error", "") in ["room-closed", "invalid-room", "invalid-token"]:
                self.room_close_and_reintake(self.room)
            return deliver

    def get_qrcode_from_base64(self, qrbase64):
        try:
            data = qrbase64.split(",")[1]
        except IndexError:
            data = qrbase64
        img = Image.open(BytesIO(base64.b64decode(data)))
        code = zbarlight.scan_codes(["qrcode"], img)[0]
        return code

    def generate_qrcode(self, code):
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_H,
            box_size=40,
            border=5,
        )

        qr.add_data(code)
        qr.make(fit=True)
        img = qr.make_image()

        buffered = BytesIO()
        img.save(buffered, format="PNG")
        img_str = base64.b64encode(buffered.getvalue()).decode("utf-8")
        return img_str

    def outcome_admin_message(self, text):
        managers = self.connector.get_managers()
        managers_channel = self.connector.get_managers_channel(as_string=False)
        if settings.DEBUG:
            print("GOT MANAGERS: ", managers)
            print("GOT CHANNELS: ", managers_channel)
        if self.get_rocket_client(bot=True):
            # send to the managers
            im_room = self.rocket.im_create(username="", usernames=managers)
            response = im_room.json()
            if settings.DEBUG:
                print("CREATE ADMIN ROOM TO OUTCOME", im_room.json())
            text_message = ":rocket: CONNECT {0}".format(text)
            if response["success"]:
                if settings.DEBUG:
                    print("SENDING ADMIN MESSAGE")
                self.rocket.chat_post_message(
                    alias=self.connector.name,
                    text=text_message,
                    room_id=response["room"]["rid"],
                )
            # send to managers channel
            for manager_channel in managers_channel:
                manager_channel_message = self.rocket.chat_post_message(
                    text=text_message, channel=manager_channel.replace("#", "")
                )
                if manager_channel_message.ok:
                    self.logger_info(
                        "OK! manager_channel_message payload received: {0}".format(
                            manager_channel_message.json()
                        )
                    )
                else:
                    self.logger_info(
                        "ERROR! manager_channel_message: {0}".format(
                            manager_channel_message.json()
                        )
                    )

    def get_visitor_name(self):
        try:
            name = self.message.get("data", {}).get("sender", {}).get("name")
        except IndexError:
            name = "Duda Nogueira"
        return name

    def get_visitor_username(self):
        try:
            visitor_username = "whatsapp:{0}".format(
                # works for wa-automate
                self.message.get("data", {}).get("from")
            )
        except IndexError:
            visitor_username = "channel:visitor-username"
        return visitor_username

    def get_visitor_phone(self):
        try:
            visitor_phone = self.message.get("data", {}).get("from").split("@")[0]
        except IndexError:
            visitor_phone = "553199999999"
        return visitor_phone

    def get_visitor_json(self, department=None):
        visitor_name = self.get_visitor_name()
        visitor_username = self.get_visitor_username()
        visitor_phone = self.get_visitor_phone()
        visitor_token = self.get_visitor_token()
        if not department:
            department = self.connector.department
        connector_name = self.connector.name

        visitor = {
            "username": visitor_username,
            "token": visitor_token,
            "phone": visitor_phone,
            "customFields": [
                {
                    "key": "connector_name",
                    "value": connector_name,
                    "overwrite": True,
                },
            ],
        }
        if department:
            visitor["department"] = department

        if visitor_name:
            visitor["customFields"].append(
                {
                    "key": "whatsapp_name",
                    "value": visitor_name,
                    "overwrite": self.config.get("overwrite_custom_fields", True),
                }
            )

        if visitor_phone:
            visitor["customFields"].append(
                {
                    "key": "whatsapp_number",
                    "value": visitor_phone,
                    "overwrite": self.config.get("overwrite_custom_fields", True),
                }
            )

        if visitor_name and not self.config.get("supress_visitor_name", False):
            visitor["name"] = visitor_name

        if settings.DEBUG:
            print("GOT VISITOR JSON: ", visitor)

        return visitor

    def get_incoming_visitor_id(self):
        if self.message.get("event") == "onIncomingCall":
            # incoming call get different ID
            return self.message.get("data", {}).get("peerJid")
        else:
            return self.message.get("data", {}).get("from")

    def get_visitor_id(self):
        if self.type == "incoming":
            return self.get_incoming_visitor_id()
        return self.message.get("visitor", {}).get("token").split(":")[1]

    def get_visitor_token(self):
        try:
            # this works for wa-automate EASYAPI
            visitor_id = self.get_visitor_id()
            visitor_id = "whatsapp:{0}".format(visitor_id)
            return visitor_id
        except IndexError:
            return "channel:visitor-id"

    def get_room(self, department=None):
        room = None
        room_created = False
        connector_token = self.get_visitor_token()
        try:
            room = LiveChatRoom.objects.get(
                connector=self.connector, token=connector_token, open=True
            )
            print("get_room, got: ", room)
        except LiveChatRoom.MultipleObjectsReturned:
            # this should not happen. Mitigation for issue #12
            # TODO: replicate error at development
            return (
                LiveChatRoom.objects.filter(
                    connector=self.connector, token=connector_token, open=True
                )
                .order_by("-created")
                .last()
            )
        except LiveChatRoom.DoesNotExist:
            print("get_room, didnt get for: ", connector_token)
            if self.config.get("open_room", True):
                # room not available, let's create one.
                # get the visitor json
                visitor_json = self.get_visitor_json(department)
                # get the visitor object
                visitor_object = self.rocket.livechat_register_visitor(
                    visitor=visitor_json, token=connector_token
                )
                response = visitor_object.json()
                if settings.DEBUG:
                    print("VISITOR REGISTERING: ", response)
                # we got a new room
                # this is where you can hook some "welcoming features"
                if response["success"]:
                    rc_room = self.rocket.livechat_room(token=connector_token)
                    rc_room_response = rc_room.json()
                    if settings.DEBUG:
                        print("REGISTERING ROOM, ", rc_room_response)
                    if rc_room_response["success"]:
                        room = LiveChatRoom.objects.create(
                            connector=self.connector,
                            token=connector_token,
                            room_id=rc_room_response["room"]["_id"],
                            open=True,
                        )
                        room_created = True
                    else:
                        if rc_room_response["error"] == "no-agent-online":
                            if settings.DEBUG:
                                print("Erro! No Agents Online")
        self.room = room
        if self.config.get("welcome_message"):
            # only send welcome message when
            # 1 - open_room is False and there is a welcome_message
            # 2 - open_room is True, room_created is True and there is a welcome_message
            if (
                not self.config.get("open_room", True)
                and self.config.get("welcome_message")
            ) or (
                self.config.get("open_room", True)
                and room_created
                and self.config.get("welcome_message")
            ):
                message = {"msg": self.config.get("welcome_message")}
                self.outgo_text_message(message)
                # if room was created
                if room and self.config.get(
                    "alert_agent_of_automated_message_sent", False
                ):
                    # let the agent know
                    self.outcome_text(
                        room.room_id,
                        "MESSAGE SENT: {0}".format(self.config.get("welcome_message")),
                        message_id=self.get_message_id() + "WELCOME",
                    )
        if self.config.get("welcome_vcard") != {}:
            # only send welcome vcard when
            #
            # 1 - open_room is False and there is a welcome_vcard
            # 2 - open_room is True, room_created is True and there is a welcome_vcard
            if (
                not self.config.get("open_room", True)
                and self.config.get("welcome_vcard")
            ) or (
                self.config.get("open_room", True)
                and room_created
                and self.config.get("welcome_vcard")
            ):
                payload = self.config.get("welcome_vcard")
                self.outgo_vcard(payload)
                # if room was created
                if room and self.config.get(
                    "alert_agent_of_automated_message_sent", False
                ):
                    # let the agent know
                    self.outcome_text(
                        room_id=room.room_id,
                        text="VCARD SENT: {0}".format(self.config.get("welcome_vcard")),
                        message_id=self.get_message_id() + "VCARD",
                    )
        if self.message_object:
            self.message_object.room = room
            self.message_object.save()

        return room

    def room_close_and_reintake(self, room):
        if settings.DEBUG:
            print("ROOM IS CLOSED. CLOSING AND REINTAKING")
        room.open = False
        room.save()
        # reintake the message
        # so now it can go to a new room
        self.incoming()

    def room_send_text(self, room_id, text, message_id=None):
        if settings.DEBUG:
            print("SENDING MESSAGE TO ROOM ID {0}: {1}".format(room_id, text))
        if not message_id:
            message_id = self.get_message_id()
        rocket = self.get_rocket_client()
        response = rocket.livechat_message(
            token=self.get_visitor_token(),
            rid=room_id,
            msg=text,
            _id=message_id,
        )
        if settings.DEBUG:
            self.logger_info("MESSAGE SENT. RESPONSE: {0}".format(response.json()))
        return response

    def register_message(self, type=None):
        if settings.DEBUG:
            print("REGISTERING MESSAGE: ", self.message)
        try:
            if not type:
                type = self.type
            self.message_object, created = self.connector.messages.get_or_create(
                envelope_id=self.get_message_id(), type=type
            )
            self.message_object.raw_message = self.message
            if not self.message_object.room:
                self.message_object.room = self.room
            self.message_object.save()
            if created:
                self.logger_info(
                    "NEW MESSAGE REGISTERED: {0}".format(self.message_object.id)
                )
            else:
                self.logger_info(
                    "EXISTING MESSAGE REGISTERED: {0}".format(self.message_object.id)
                )
            return self.message_object, created
        except IntegrityError:
            raise
            self.logger_info(
                "CANNOT CREATE THIS MESSAGE AGAIN: {0}".format(self.get_message_id())
            )
            return "", False

    def get_message_id(self):
        if self.type == "incoming":
            return self.get_incoming_message_id()
        if self.type == "ingoing":
            # rocketchat message id
            if self.message["messages"]:
                rc_message_id = self.message["messages"][0]["_id"]
                return rc_message_id
            # other types of message
            if self.message.get("_id"):
                return self.message.get("_id")

        # last resource
        return self.get_incoming_message_id()

    def get_incoming_message_id(self):
        # this works for wa-automate EASYAPI
        try:
            message_id = self.message.get("data", {}).get("id")
        except IndexError:
            # for sake of forgiveness, lets make it random
            message_id = "".join(random.choice(string.ascii_letters) for i in range(10))
        print("MESSAGE ID ", message_id)
        return message_id

    def get_message_body(self):
        try:
            # this works for wa-automate EASYAPI
            message_body = self.message.get("data", {}).get("body")
        except IndexError:
            message_body = "New Message: {0}".format(
                "".join(random.choice(string.ascii_letters) for i in range(10))
            )
        return message_body

    def get_rocket_client(self, bot=False):
        # this will prevent multiple client initiation at the same
        # Classe initiation
        if not self.rocket:
            try:
                self.rocket = self.connector.server.get_rocket_client(bot=bot)
            except requests.exceptions.ConnectionError:
                # do something when rocketdown
                self.rocket_down()
                self.rocket = False
        return self.rocket

    def rocket_down(self):
        if settings.DEBUG:
            print("DO SOMETHING FOR WHEN ROCKETCHAT SERVER IS DOWN")

    def joypixel_to_unicode(self, content):
        return emojipy.Emoji().shortcode_to_unicode(content)

    # API METHODS
    def decrypt_media(self, message_id=None):
        if not message_id:
            message_id = self.get_message_id()
        url_decrypt = "{0}/decryptMedia".format(self.config["endpoint"])
        payload = {"args": {"message": message_id}}
        s = self.get_request_session()
        decrypted_data_request = s.post(url_decrypt, json=payload)
        # get decrypted data
        data = None
        if decrypted_data_request.ok:
            response = decrypted_data_request.json().get("response", None)
            if settings.DEBUG:
                print("DECRYPTED DATA: ", response)
            if response:
                data = response.split(",")[1]
        return data

    def close_room(self):
        if self.room:
            if settings.DEBUG:
                print("Closing Message...")
            self.room.open = False
            self.room.save()
            self.post_close_room()

    def post_close_room(self):
        """
        Method that runs after the room is closed
        """
        if settings.DEBUG:
            print("Do stuff after the room is closed")

    def ingoing(self):
        """
        this method will process the outcoming messages
        comming from Rocketchat, and deliver to the connector
        """
        self.logger_info("RECEIVED: {0}".format(self.message))
        # Session start
        if self.message.get("type") == "LivechatSessionStart":
            if settings.DEBUG:
                print("LivechatSessionStart")
            # some welcome message may fit here
        if self.message.get("type") == "LivechatSession":
            #
            # This message is sent at the end of the chat,
            # with all the chats from the session.
            # if the Chat Close Hook is On
            if settings.DEBUG:
                print("LivechatSession")
        if self.message.get("type") == "LivechatSessionTaken":
            #
            # This message is sent when the message if taken
            # message, created = self.register_message()
            self.handle_livechat_session_taken()

        if self.message.get("type") == "LivechatSessionForwarded":
            #
            # This message is sent when the message if Forwarded
            if settings.DEBUG:
                print("LivechatSessionForwarded")
        if self.message.get("type") == "LivechatSessionQueued":
            #
            # This message is sent when the message if Forwarded
            if settings.DEBUG:
                print("LivechatSessionQueued")
        if self.message.get("type") == "Message":
            message, created = self.register_message()

            # prepare message to be sent to client
            for message in self.message.get("messages", []):
                agent_name = self.get_agent_name(message)
                # closing message
                if message.get("closingMessage"):
                    if self.connector.config.get(
                        "force_close_message",
                    ):
                        message["msg"] = self.connector.config["force_close_message"]
                    if message.get("msg"):
                        if self.connector.config.get("add_agent_name_at_close_message"):
                            self.outgo_text_message(message, agent_name=agent_name)
                        else:
                            self.outgo_text_message(message)
                        self.close_room()
                    # closing message without message
                    else:
                        self.message_object.delivered = True
                        self.message_object.save()
                else:
                    # regular message, maybe with attach
                    if message.get("attachments", {}):
                        # send file
                        self.outgo_file_message(message, agent_name=agent_name)
                    else:
                        self.outgo_text_message(message, agent_name=agent_name)

    def get_agent_name(self, message):
        agent_name = message.get("u", {}).get("name", {})
        return self.change_agent_name(agent_name)

    def change_agent_name(self, agent_name):
        return agent_name

    def outgo_text_message(self, message, agent_name=None):
        """
        this method should be overwritten to send the message back to the client
        """
        if agent_name:
            self.logger_info(
                "OUTGOING MESSAGE {0} FROM AGENT {1}".format(message, agent_name)
            )
        else:
            self.logger_info("OUTGOING MESSAGE {0}".format(message))
        return True

    def outgo_vcard(self, vcard_json):
        self.logger_info("OUTGOING VCARD {0}".format(vcard_json))

    def handle_incoming_call(self):
        if self.config.get("auto_answer_incoming_call"):
            self.logger_info(
                "auto_answer_incoming_call: {0}".format(
                    self.config.get("auto_answer_incoming_call")
                )
            )
            message = {"msg": self.config.get("auto_answer_incoming_call")}
            self.outgo_text_message(message)
        if self.config.get("convert_incoming_call_to_text"):
            if self.room:
                self.outcome_text(
                    self.room.room_id,
                    text=self.config.get("convert_incoming_call_to_text"),
                )

    def handle_ptt(self):
        if self.config.get("auto_answer_on_audio_message"):
            self.logger_info(
                "auto_answer_on_audio_message: {0}".format(
                    self.config.get("auto_answer_on_audio_message")
                )
            )
            message = {"msg": self.connector.config.get("auto_answer_on_audio_message")}
            self.outgo_text_message(message)
        if self.config.get("convert_incoming_audio_to_text"):
            if self.room:
                self.outcome_text(
                    self.room.room_id,
                    text=self.config.get("convert_incoming_audio_to_text"),
                )

    def handle_livechat_session_taken(self):
        if self.config.get("session_taken_alert_template"):
            template = Template(self.config.get("session_taken_alert_template"))
            context = Context(self.message)
            message = template.render(context)
            message_payload = {"msg": message}
            if (
                self.config.get("alert_agent_of_automated_message_sent", False)
                and self.room
            ):
                # let the agent know
                self.outcome_text(
                    self.room.room_id,
                    "MESSAGE SENT: {0}".format(message),
                    message_id=self.get_message_id() + "SESSION_TAKEN",
                )
            return self.outgo_text_message(message_payload).json()


class BaseConnectorConfigForm(forms.Form):
    def __init__(self, *args, **kwargs):
        # get the instance connector
        self.connector = kwargs.pop("connector")
        # pass the connector config as initial
        super().__init__(*args, **kwargs, initial=self.connector.config)

    def save(self):
        for field in self.cleaned_data.keys():
            if self.cleaned_data[field]:
                self.connector.config[field] = self.cleaned_data[field]
            else:
                if self.connector.config.get(field):
                    # if is a boolean field, mark as false
                    # else, delete
                    if type(self.fields[field]) == forms.fields.BooleanField:
                        self.connector.config[field] = False
                    else:
                        del self.connector.config[field]
            self.connector.save()

    open_room = forms.BooleanField(
        required=False, initial=True, help_text="Uncheck to avoid creating a room"
    )
    timezone = forms.CharField(help_text="Timezone for this connector", required=False)
    force_close_message = forms.CharField(
        help_text="Force this message on close", required=False
    )
    outcome_attachment_description_as_new_message = forms.BooleanField(
        required=False,
        help_text="This might be necessary for the bot to react accordingly",
    )
    add_agent_name_at_close_message = forms.BooleanField(required=False)
    overwrite_custom_fields = forms.BooleanField(
        required=False, help_text="overwrite custom fields on new visitor registration"
    )
    alert_agent_of_automated_message_sent = forms.BooleanField(
        required=False,
        help_text="Alert the agent whenever you send an automated text."
        + "WARNING: this option will cause a bot to react to those messages.",
    )
    auto_answer_incoming_call = forms.CharField(
        help_text="Auto answer this message on incoming call", required=False
    )
    convert_incoming_call_to_text = forms.CharField(
        help_text="Convert an Incoming Call to this text (can be used to force a bot reaction)",
        required=False,
    )
    auto_answer_on_audio_message = forms.CharField(
        required=False,
        help_text="Auto answer with this message when a user end audio (PTT)",
    )
    convert_incoming_audio_to_text = forms.CharField(
        required=False,
        help_text="Convert a user audio to this message (can be used to force a bot reaction)",
    )
    welcome_message = forms.CharField(
        help_text="Auto answer this message as Welcome Message", required=False
    )
    welcome_vcard = forms.JSONField(
        required=False, initial={}, help_text="The Payload for a Welcome Vcard"
    )
    session_taken_alert_template = forms.CharField(
        required=False,
        help_text="Template to use for the alert session taken. eg. You are now talking with {{agent.name}}",
    )
