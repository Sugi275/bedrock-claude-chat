import json
import logging
import os
from datetime import datetime

import boto3
from auth import verify_token
from repositories.conversation import store_conversation
from repositories.model import ContentModel, MessageModel
from route_schema import ChatInputWithToken
from ulid import ULID
from usecase import get_invoke_payload, prepare_conversation
from utils import get_bedrock_client

client = get_bedrock_client()

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


def generate_chunk(stream) -> bytes:
    if stream:
        for event in stream:
            chunk = event.get("chunk")
            if chunk:
                chunk_bytes = chunk.get("bytes")
                yield chunk_bytes


def handler(event, context):
    route_key = event["requestContext"]["routeKey"]

    if route_key == "$connect":
        # NOTE: Authentication is done at each message
        return {"statusCode": 200, "body": "Connected."}

    connection_id = event["requestContext"]["connectionId"]
    domain_name = event["requestContext"]["domainName"]
    stage = event["requestContext"]["stage"]
    message = event["body"]
    endpoint_url = f"https://{domain_name}/{stage}"
    gatewayapi = boto3.client("apigatewaymanagementapi", endpoint_url=endpoint_url)

    chat_input = ChatInputWithToken(**json.loads(message))
    logger.debug(f"Received chat input: {chat_input}")

    try:
        # Verify JWT token
        decoded = verify_token(chat_input.token)
    except Exception as e:
        print(f"Invalid token: {e}")
        return {"statusCode": 403, "body": "Invalid token."}

    user_id = decoded["sub"]
    conversation = prepare_conversation(user_id, chat_input)
    payload = get_invoke_payload(conversation, chat_input)

    try:
        # Invoke bedrock streaming api
        response = client.invoke_model_with_response_stream(
            body=payload["body"],
            modelId=payload["model_id"],
            accept=payload["accept"],
            contentType=payload["content_type"],
        )
    except Exception as e:
        print(f"Failed to invoke bedrock: {e}")
        return {"statusCode": 500, "body": "Failed to invoke bedrock."}

    stream = response.get("body")
    completions = []
    for chunk in generate_chunk(stream):
        try:
            # Send completion
            gatewayapi.post_to_connection(ConnectionId=connection_id, Data=chunk)
            chunk_data = json.loads(chunk.decode("utf-8"))
            completions.append(chunk_data["completion"])
        except Exception as e:
            print(f"Failed to post message: {str(e)}")
            return {"statusCode": 500, "body": "Failed to send message to connection."}

    concatenated = "".join(completions)

    # Append entire completion as the last message
    message = MessageModel(
        id=str(ULID()),
        role="assistant",
        content=ContentModel(content_type="text", body=concatenated),
        model=chat_input.message.model,
        create_time=datetime.now().timestamp(),
    )
    conversation.messages.append(message)

    # Persist conversation
    store_conversation(user_id, conversation)

    return {"statusCode": 200, "body": json.dumps({"conversationId": conversation.id})}
