import base64
import json
import os
import time
import urllib.parse as urlparse

import requests
from django import forms
from django.conf import settings
from django.core import validators
from django.http import JsonResponse

from .base import BaseConnectorConfigForm
from .base import Connector as ConnectorBase


class Connector(ConnectorBase):
    #
    # SESSION MANAGEMENT
    #
    def initialize(self):
        endpoint_create = "{}/instance/create".format(
            self.config.get("endpoint"),
        )
        secret_key = self.config.get("secret_key")
        webhook_url = self.config.get("webhook")
        instance_name = self.config.get("instance_name")
        headers = {"apiKey": secret_key, "Content-Type": "application/json"}
        # GET {{baseUrl}}/instance/create with { "instanceName": "codechat"}
        payload = {"instanceName": instance_name}
        headers = {"Content-Type": "application/json", "apikey": secret_key}

        create_instance_response = requests.request(
            "POST",
            endpoint_create,
            json=payload,
            headers=headers,
        )
        output = {}
        output["instance_create"] = {
            "endpoint": endpoint_create,
            **create_instance_response.json(),
        }
        # GET {{baseUrl}}/instance/connect/{{instance}}

        endpoint_connect = "{}/instance/connect/{}".format(
            self.config.get("endpoint"),
            instance_name,
        )
        connect_instance_response = requests.request(
            "GET",
            endpoint_connect,
            json=payload,
            headers=headers,
        )
        output["instance_connect"] = {
            "endpoint": endpoint_connect,
            **connect_instance_response.json(),
        }
        # POST {{baseUrl}}/webhook/set/{{instance}}
        endpoint_webhook_set = "{}/webhook/set/{}".format(
            self.config.get("endpoint"),
            instance_name,
        )
        payload = {"enabled": True, "url": webhook_url}
        connect_instance_response = requests.request(
            "POST",
            endpoint_webhook_set,
            json=payload,
            headers=headers,
        )
        output["instance_webhook"] = {
            "endpoint": endpoint_webhook_set,
            **connect_instance_response.json(),
        }
        return output

    def status_session(self):
        if self.config.get("endpoint", None):
            # GET {{baseUrl}}/instance/connectionState/{{instance}}
            secret_key = self.config.get("secret_key")
            instance_name = self.config.get("instance_name")
            headers = {"apiKey": secret_key, "Content-Type": "application/json"}

            endpoint_status = "{}/instance/connect/{}".format(
                self.config.get("endpoint"),
                instance_name,
            )
            status_instance_response = requests.request(
                "GET",
                endpoint_status,
                headers=headers,
            )
            if status_instance_response:
                return status_instance_response.json()
            return {"error": "no endpoint"}
        else:
            return {"error": "no endpoint"}

    def close_session(self):
        # DELETE {{baseUrl}}/instance/logout/{{instance}}
        secret_key = self.config.get("secret_key")
        instance_name = self.config.get("instance_name")
        headers = {"apiKey": secret_key, "Content-Type": "application/json"}
        endpoint_status = "{}/instance/logout/{}".format(
            self.config.get("endpoint"),
            instance_name,
        )
        status_instance_response = requests.request(
            "DELETE",
            endpoint_status,
            headers=headers,
        )
        return status_instance_response.ok

    #
    # INCOMING HANDLERS
    #

    def incoming(self):
        message = json.dumps(self.message)
        self.logger_info(f"INCOMING MESSAGE: {message}")
        #
        # qrcode reading
        #
        if self.message.get("event") == "qrcode.updated":
            base64 = self.message.get("data", {}).get("qrcode", {}).get("base64")
            if base64:
                self.outcome_qrbase64(base64)
        #
        # connection update
        #
        if self.message.get("event") == "connection.update":
            data = self.message.get("data", {})
            text = f"*CONNECTOR NAME:* {self.connector.name} > {json.dumps(data)}"
            if data.get("state") == "open":
                text = text + "\n" + " ✅ " * 6
            self.outcome_admin_message(text)
        #
        # message upsert
        #
        if self.message.get("event") == "messages.upsert":
            department = None
            message, created = self.register_message()
            if not message.delivered:
                message = self.message.get("data", {}).get("message", {})
                # get room
                room = self.get_room(department)
                if not room:
                    return JsonResponse({"message": "no room generated"})
                #
                # oucome if text
                #
                if message.get("extendedTextMessage"):
                    text = (
                        self.message.get("data", {})
                        .get("message", {})
                        .get("extendedTextMessage")
                        .get("text")
                    )
                    deliver = self.outcome_text(room.room_id, text)
                    print(deliver)
                #
                # outcome if image
                #
                if (
                    message.get("imageMessage")
                    or message.get("audioMessage")
                    or message.get("videoMessage")
                    or message.get("stickerMessage")
                    or message.get("documentWithCaptionMessage")
                    or message.get("documentMessage")
                    or message.get("contactsArrayMessage")
                    or message.get("locationMessage")
                    or message.get("stickerMessage")
                ):
                    filename = None
                    caption = None
                    mime = None

                    # files that are actually text
                    if message.get("contactsArrayMessage"):
                        for contact in message.get("contactsArrayMessage").get(
                            "contacts",
                        ):
                            sent = self.outcome_text(
                                room_id=room.room_id,
                                text=f"{contact.get('displayName')}\n{contact.get('vcard')}",
                            )
                        if sent.ok:
                            self.message_object.delivered = True
                            self.message_object.save()
                        return JsonResponse({})

                    if message.get("locationMessage"):
                        message_type = "locationMessage"
                        text = (
                            f"Lat: {message.get(message_type).get('degreesLatitude')} "
                        )
                        +"Lon: {message.get(message_type).get('degreesLongitude')}"
                        self.outcome_text(room.room_id, text=text)
                        return JsonResponse({})

                    # "real" files
                    if message.get("imageMessage"):
                        message_type = "imageMessage"

                    if message.get("audioMessage"):
                        message_type = "audioMessage"

                    if message.get("videoMessage"):
                        message_type = "videoMessage"

                    if message.get("stickerMessage"):
                        # TODO: sticker not supported
                        return JsonResponse({})
                        message_type = "stickerMessage"

                    if message.get("documentWithCaptionMessage"):
                        message_type = "documentWithCaptionMessage"
                        filename = (
                            message.get(message_type)
                            .get("message")
                            .get("documentMessage")
                            .get("title")
                        )
                        caption = (
                            message.get(message_type)
                            .get("message")
                            .get("documentMessage")
                            .get("caption")
                        )
                        mime = (
                            message.get(message_type)
                            .get("message")
                            .get("documentMessage")
                            .get("mimetype")
                        )
                    if message.get("documentMessage"):
                        message_type = "documentMessage"
                        filename = message.get(message_type).get("title")
                        mime = message.get(message_type).get("mimetype")

                    if not caption:
                        # get default caption
                        caption = message.get(message_type, {}).get("caption")

                    if not mime:
                        mime = message.get(message_type).get("mimetype")

                    payload = {
                        "key": {
                            "id": self.message.get("data", {}).get("key", {}).get("id"),
                        },
                    }
                    url = self.connector.config[
                        "endpoint"
                    ] + "/chat/getBase64FromMediaMessage/{}".format(
                        self.connector.config["instance_name"],
                    )
                    headers = {
                        "apiKey": self.config.get("secret_key"),
                        "Content-Type": "application/json",
                    }
                    media_body = requests.post(url, headers=headers, json=payload)

                    if media_body.ok:
                        body = media_body.json().get("base64")
                        filename = filename

                        file_sent = self.outcome_file(
                            body,
                            room.room_id,
                            mime,
                            description=caption,
                            filename=filename,
                        )
                        self.logger_info(
                            f"Outcoming message. url {url}, file sent: {file_sent.json()}",
                        )
                        if file_sent.ok:
                            self.message_object.delivered = True
                            self.message_object.save()
                    else:
                        self.logger_info(
                            f"GETTOMG message MEDIA ERROR. url {url}, file sent: {media_body.json()} media_body",
                        )

            else:
                self.logger_info(
                    f"Message Object {message.id} Already delivered. Ignoring",
                )

        return JsonResponse({})

    #
    # OUTGO
    #

    def outgo_text_message(self, message, agent_name=None):
        if type(message) == str:
            content = message
        else:
            content = message["msg"]
        payload = {
            "number": self.get_ingoing_visitor_phone(),
            "options": {"delay": self.connector.config.get("send_message_delay", 1200)},
            "textMessage": {"text": content},
        }
        url = self.connector.config["endpoint"] + "/message/SendText/{}".format(
            self.connector.config["instance_name"],
        )
        headers = {
            "apiKey": self.config.get("secret_key"),
            "Content-Type": "application/json",
        }
        sent = requests.post(url, headers=headers, json=payload)
        self.logger_info(
            f"OUTGO TEXT MESSAGE. URL: {url}. PAYLOAD {payload} RESULT: {sent.json()}",
        )
        return sent

    def outgo_file_message(self, message, file_url=None, mime=None, agent_name=None):
        caption = None
        if not file_url:
            file_url = (
                self.connector.server.url
                + message["attachments"][0]["title_link"]
                + "?"
                + urlparse.urlparse(message["fileUpload"]["publicFilePath"]).query
            )
            caption = message["attachments"][0].get("description")
        content = base64.b64encode(requests.get(file_url).content).decode("utf-8")
        file_name = os.path.basename(file_url).split("?")[0]
        if not mime:
            mime = self.message["messages"][0]["fileUpload"]["type"]
        mediatype = mime.split("/")[0]
        if mediatype == "application":
            mediatype = "document"
        payload = {
            "number": self.get_ingoing_visitor_phone(),
            "options": {"delay": self.connector.config.get("send_message_delay", 1200)},
            "mediaMessage": {
                "mediatype": mediatype,
                "fileName": file_name,
                "media": content,
            },
        }
        if caption:
            payload["mediaMessage"]["caption"] = str(caption)
        url = self.connector.config["endpoint"] + "/message/SendMedia/{}".format(
            self.connector.config["instance_name"],
        )
        headers = {
            "apiKey": self.config.get("secret_key"),
            "Content-Type": "application/json",
        }
        sent = requests.post(url, headers=headers, json=payload)
        self.logger_info(
            f"OUTGOING FILE. URL: {url}. PAYLOAD {payload} response: {sent.json()}",
        )
        if sent.ok:
            # Audio doesnt have caption, so outgo as message
            if mediatype == "audio" and caption:
                self.outgo_text_message(caption)
            timestamp = int(time.time())
            if settings.DEBUG:
                self.logger.info(f"RESPONSE OUTGOING FILE to url {url}: {sent.json()}")
            self.message_object.payload[timestamp] = payload
            self.message_object.delivered = True
            self.message_object.response[timestamp] = sent.json()
            self.message_object.save()
            # self.send_seen()
        return sent

    #
    # MESSAGE METADA DATA
    #
    def get_incoming_message_id(self):
        id = None
        if self.message.get("event") == "messages.upsert":
            id = self.message.get("data", {}).get("key", {}).get("id")
        return id

    def get_visitor_name(self):
        name = self.message.get("data", {}).get("pushName")
        if name:
            return name
        return None

    def get_visitor_phone(self):
        remoteJid = self.message.get("data", {}).get("key", {}).get("remoteJid")
        if remoteJid:
            return remoteJid.split("@")[0]

        return None

    def get_visitor_username(self):
        visitor_username = f"whatsapp:{self.get_visitor_phone()}@c.us"
        return visitor_username

    def get_incoming_visitor_id(self):
        return self.get_visitor_phone() + "@c.us"

    # def get_visitor_avatar_url(self):
    #     secret_key = self.config.get("secret_key")
    #     headers = {"apiKey": secret_key, "Content-Type": "application/json"}
    #     url = self.connector.config["endpoint"] + "/chat/fetchProfilePictureUrl/{}".format(
    #         self.connector.config["instance_name"]
    #     )
    #     payload = {
    #     "number": self.get_visitor_phone()
    #     }
    #     headers = {
    #         "apiKey": self.config.get("secret_key"),
    #         "Content-Type": "application/json",
    #     }
    #     profile_picture_request = requests.post(url, headers=headers, json=payload)
    #     if profile_picture_request.ok:
    #         profile_url = profile_picture_request.json().get("profilePictureUrl")
    #         self.logger_info(f"GOT PROFILE URL: {profile_url}")
    #         return profile_picture_request.json().get("profilePictureUrl")


class ConnectorConfigForm(BaseConnectorConfigForm):
    webhook = forms.CharField(
        help_text="Where WPPConnect will send the events",
        required=True,
        initial="",
    )
    endpoint = forms.CharField(
        help_text="Where your WPPConnect is installed",
        required=True,
        initial="http://codechat:8083",
    )
    secret_key = forms.CharField(
        help_text="The secret ApiKey for your CodeChat instance",
        required=True,
    )
    instance_name = forms.CharField(
        help_text="CodeChat instance name",
        validators=[validators.validate_slug],
    )

    send_message_delay = forms.IntegerField(
        help_text="CodeChat delay to send message. Defaults to 1200",
        initial=1200,
    )

    field_order = [
        "webhook",
        "endpoint",
        "secret_key",
        "instance_name",
        "send_message_delay",
    ]
