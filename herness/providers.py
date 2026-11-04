"""Native provider protocols normalized to Herness's persisted message format.

Provider thought/reasoning text is deliberately never copied into user-visible
messages. Gemini's opaque thought signatures are retained only for tool replay.
"""
from __future__ import annotations

import json
import urllib.parse
import uuid

from .core import OpenAIClient, ModelError


def _call(name, arguments, identity=None, signature=None):
    call = {'id': identity or 'call_' + uuid.uuid4().hex, 'type': 'function',
            'function': {'name': name, 'arguments': arguments if isinstance(arguments, str) else json.dumps(arguments)}}
    if signature:
        call['thought_signature'] = signature
    return call


def _parts(content):
    return content if isinstance(content, list) else [{'type': 'text', 'text': content or ''}]


def _image(part):
    url = part.get('image_url', {}).get('url', '')
    if not url.startswith('data:') or ';base64,' not in url:
        raise ModelError('This provider requires image attachments as base64 data URLs.')
    mime, data = url[5:].split(';base64,', 1)
    return mime, data


class NativeClient(OpenAIClient):
    def complete(self, messages):
        self.last_usage = {}
        try:
            with self._open(self.payload(messages)) as response:
                return self.parse(json.loads(response.read(4_000_001)))
        except (ValueError, TypeError, KeyError, IndexError):
            raise ModelError('The provider returned an invalid response.') from None

    def probe(self):
        cap, schemas = self.max_output_tokens, self.tool_schemas
        self.max_output_tokens, self.tool_schemas = 32, []
        try:
            message = self.complete([{'role': 'user', 'content': 'Reply with OK.'}])
            if not message.get('content'):
                raise ModelError('The provider did not return text for the connection test.')
        finally:
            self.max_output_tokens, self.tool_schemas = cap, schemas
        return 'Connection successful. The selected model responded.'

    def events(self, messages, cancel):
        self.last_usage = {}
        self._active_cancel = cancel
        response, sock = None, None
        try:
            cancel.check()
            response = self._open(self.payload(messages, stream=True))
            sock = cancel.register(response)
            received = 0
            data_lines = []
            for raw in response:
                cancel.check()
                received += len(raw)
                if received > 8_000_000 or len(raw) > 2_000_000:
                    raise ModelError('Model stream exceeded the response size limit.')
                line = raw.decode('utf-8').rstrip('\r\n')
                if line.startswith('data:'):
                    data_lines.append(line[5:].lstrip())
                elif not line and data_lines:
                    data = '\n'.join(data_lines)
                    data_lines = []
                    if data != '[DONE]':
                        yield json.loads(data)
            if data_lines:
                data = '\n'.join(data_lines)
                if data != '[DONE]':
                    yield json.loads(data)
            cancel.check()
        except (ValueError, TypeError, KeyError, IndexError, UnicodeError):
            cancel.check()
            raise ModelError('The provider returned an invalid streaming response.') from None
        except OSError:
            cancel.check()
            raise ModelError('The model stream lost its connection.') from None
        finally:
            cancel.unregister(sock)
            self._active_cancel = None
            if response:
                response.close()


class AnthropicClient(NativeClient):
    protocol = 'anthropic'

    def __init__(self, model, base_url='https://api.anthropic.com/v1', api_key=''):
        super().__init__(model, base_url, api_key)
        self.url = self.base_url + '/messages'

    def request_headers(self):
        return {'Content-Type': 'application/json', 'anthropic-version': '2023-06-01', 'x-api-key': self.api_key}

    def payload(self, messages, stream=False):
        output, system = [], []
        for message in messages:
            role = message['role']
            if role == 'system':
                system.append(message.get('content') or '')
                continue
            if role == 'tool':
                role, blocks = 'user', [{'type': 'tool_result', 'tool_use_id': message['tool_call_id'], 'content': message['content']}]
            else:
                blocks = []
                for part in _parts(message.get('content')):
                    if part['type'] == 'text' and part.get('text'):
                        blocks.append({'type': 'text', 'text': part['text']})
                    elif part['type'] == 'image_url':
                        mime, data = _image(part)
                        blocks.append({'type': 'image', 'source': {'type': 'base64', 'media_type': mime, 'data': data}})
                for call in message.get('tool_calls') or []:
                    blocks.append({'type': 'tool_use', 'id': call['id'], 'name': call['function']['name'],
                                   'input': json.loads(call['function']['arguments'])})
            if not blocks:
                blocks = [{'type': 'text', 'text': '(empty message)'}]
            if output and output[-1]['role'] == role:
                output[-1]['content'].extend(blocks)
            else:
                output.append({'role': role, 'content': blocks})
        payload = {'model': self.model, 'system': '\n\n'.join(system), 'messages': output,
                   'max_tokens': self.max_output_tokens or 4096, 'stream': stream}
        if self.tool_schemas:
            payload['tools'] = [{'name': item['function']['name'], 'description': item['function'].get('description', ''),
                                 'input_schema': item['function']['parameters']} for item in self.tool_schemas]
        return payload

    def parse(self, data):
        if data.get('stop_reason') == 'max_tokens':
            raise ModelError('The provider stopped the response at its output limit.')
        text, calls = [], []
        for block in data['content']:
            if block['type'] == 'text':
                text.append(block['text'])
            elif block['type'] == 'tool_use':
                calls.append(_call(block['name'], block['input'], block['id']))
        self.last_usage = data.get('usage') or {}
        return self.validate({'role': 'assistant', 'content': ''.join(text) or None, 'tool_calls': calls})

    def stream(self, messages, on_delta, cancel):
        text, calls, finished = '', {}, False
        for event in self.events(messages, cancel):
            kind = event['type']
            if kind == 'error':
                raise ModelError('The provider reported an error in the response stream.')
            if kind == 'message_start':
                self.last_usage.update(event['message'].get('usage') or {})
            elif kind == 'content_block_start':
                block = event['content_block']
                if block['type'] == 'tool_use':
                    calls[event['index']] = _call(block['name'], block.get('input') or '', block['id'])
                elif block['type'] == 'text' and block.get('text'):
                    text += block['text']
                    on_delta(block['text'])
            elif kind == 'content_block_delta':
                delta = event['delta']
                if delta['type'] == 'text_delta':
                    text += delta['text']
                    on_delta(delta['text'])
                elif delta['type'] == 'input_json_delta':
                    calls[event['index']]['function']['arguments'] += delta['partial_json']
            elif kind == 'message_delta':
                self.last_usage.update(event.get('usage') or {})
                if event.get('delta', {}).get('stop_reason') == 'max_tokens':
                    raise ModelError('The provider stopped the response at its output limit.')
            elif kind == 'message_stop':
                finished = True
        if not finished:
            raise ModelError('The model stream ended before completion.')
        for call in calls.values():
            if not call['function']['arguments']:
                call['function']['arguments'] = '{}'
        return self.validate({'role': 'assistant', 'content': text or None, 'tool_calls': [calls[key] for key in sorted(calls)]})


class GeminiClient(NativeClient):
    protocol = 'gemini'

    def __init__(self, model, base_url='https://generativelanguage.googleapis.com/v1beta', api_key=''):
        super().__init__(model, base_url, api_key)

    def request_headers(self):
        return {'Content-Type': 'application/json', 'x-goog-api-key': self.api_key}

    def payload(self, messages, stream=False):
        self.url = self.base_url + '/models/' + urllib.parse.quote(self.model, safe='') + (':streamGenerateContent?alt=sse' if stream else ':generateContent')
        contents, system, names = [], [], {}
        for message in messages:
            role = message['role']
            if role == 'system':
                system.append({'text': message.get('content') or ''})
                continue
            if role == 'tool':
                parts = [{'functionResponse': {'name': names.get(message['tool_call_id'], message['tool_call_id']),
                                               'response': {'result': message['content']}}}]
            else:
                parts = []
                for part in _parts(message.get('content')):
                    if part['type'] == 'text' and part.get('text'):
                        parts.append({'text': part['text']})
                    elif part['type'] == 'image_url':
                        mime, data = _image(part)
                        parts.append({'inlineData': {'mimeType': mime, 'data': data}})
                for call in message.get('tool_calls') or []:
                    names[call['id']] = call['function']['name']
                    part = {'functionCall': {'name': call['function']['name'], 'args': json.loads(call['function']['arguments'])}}
                    if call.get('thought_signature'):
                        part['thoughtSignature'] = call['thought_signature']
                    parts.append(part)
            role = 'model' if role == 'assistant' else 'user'
            if contents and contents[-1]['role'] == role:
                contents[-1]['parts'].extend(parts)
            else:
                contents.append({'role': role, 'parts': parts or [{'text': '(empty message)'}]})
        payload = {'contents': contents, 'systemInstruction': {'parts': system},
                   'generationConfig': {'maxOutputTokens': self.max_output_tokens or 4096}}
        if self.tool_schemas:
            payload['tools'] = [{'functionDeclarations': [
                {'name': item['function']['name'], 'description': item['function'].get('description', ''),
                 'parametersJsonSchema': item['function']['parameters']} for item in self.tool_schemas]}]
        return payload

    def parse(self, data):
        usage = data.get('usageMetadata') or {}
        if usage:
            self.last_usage = {'input_tokens': usage.get('promptTokenCount', 0),
                               'output_tokens': usage.get('candidatesTokenCount', 0) + usage.get('thoughtsTokenCount', 0)}
        candidates = data.get('candidates') or []
        if not candidates:
            raise ModelError('The provider returned no response candidate.')
        candidate = candidates[0]
        if candidate.get('finishReason') not in {None, 'STOP'}:
            raise ModelError('The provider stopped the response before completion.')
        text, calls = [], []
        for part in candidate.get('content', {}).get('parts', []):
            if part.get('thought'):
                continue
            if part.get('text'):
                text.append(part['text'])
            if part.get('functionCall'):
                call = part['functionCall']
                calls.append(_call(call['name'], call.get('args', {}), call.get('id'), part.get('thoughtSignature')))
        return self.validate({'role': 'assistant', 'content': ''.join(text) or None, 'tool_calls': calls})

    def stream(self, messages, on_delta, cancel):
        text, calls, finished = '', [], False
        for event in self.events(messages, cancel):
            if event.get('error'):
                raise ModelError('The provider reported an error in the response stream.')
            if event.get('usageMetadata') and not event.get('candidates'):
                usage = event['usageMetadata']
                self.last_usage = {'input_tokens': usage.get('promptTokenCount', 0), 'output_tokens': usage.get('candidatesTokenCount', 0) + usage.get('thoughtsTokenCount', 0)}
                continue
            message = self.parse(event)
            if message.get('content'):
                text += message['content']
                on_delta(message['content'])
            calls.extend(message.get('tool_calls') or [])
            finished = finished or event['candidates'][0].get('finishReason') == 'STOP'
        if not finished:
            raise ModelError('The model stream ended before completion.')
        return self.validate({'role': 'assistant', 'content': text or None, 'tool_calls': calls})


class ResponsesClient(NativeClient):
    protocol = 'responses'

    def __init__(self, model, base_url='https://api.openai.com/v1', api_key=''):
        super().__init__(model, base_url, api_key)
        self.url = self.base_url + '/responses'

    def payload(self, messages, stream=False):
        inputs = []
        for message in messages:
            role = message['role']
            if role == 'tool':
                inputs.append({'type': 'function_call_output', 'call_id': message['tool_call_id'], 'output': message['content']})
                continue
            content = []
            for part in _parts(message.get('content')):
                if part['type'] == 'text' and part.get('text'):
                    content.append({'type': 'output_text' if role == 'assistant' else 'input_text', 'text': part['text']})
                elif part['type'] == 'image_url':
                    content.append({'type': 'input_image', 'image_url': part['image_url']['url']})
            if content:
                inputs.append({'role': role, 'content': content})
            for call in message.get('tool_calls') or []:
                inputs.append({'type': 'function_call', 'call_id': call['id'], 'name': call['function']['name'], 'arguments': call['function']['arguments']})
        payload = {'model': self.model, 'input': inputs, 'stream': stream, 'store': False,
                   'max_output_tokens': self.max_output_tokens or 4096}
        if self.tool_schemas:
            payload['tools'] = [{'type': 'function', **item['function'], 'strict': False} for item in self.tool_schemas]
        return payload

    def parse(self, data):
        if data.get('status') not in {None, 'completed'} or data.get('error'):
            raise ModelError('The provider did not complete this response.')
        self.last_usage = data.get('usage') or {}
        text, calls = [], []
        for item in data['output']:
            if item['type'] == 'message':
                text.extend(part['text'] for part in item.get('content', []) if part.get('type') == 'output_text')
            elif item['type'] == 'function_call':
                calls.append(_call(item['name'], item['arguments'], item['call_id']))
        return self.validate({'role': 'assistant', 'content': ''.join(text) or None, 'tool_calls': calls})

    def stream(self, messages, on_delta, cancel):
        completed = None
        for event in self.events(messages, cancel):
            if event['type'] == 'response.output_text.delta':
                on_delta(event['delta'])
            elif event['type'] == 'response.completed':
                completed = event['response']
            elif event['type'] in {'error', 'response.failed', 'response.incomplete'}:
                raise ModelError('The provider did not complete this response.')
        if completed is None:
            raise ModelError('The model stream ended before completion.')
        return self.parse(completed)


PROTOCOLS = {'chat_completions': OpenAIClient, 'anthropic': AnthropicClient,
             'gemini': GeminiClient, 'responses': ResponsesClient}


def create_client(protocol, model, base_url, api_key=''):
    try:
        adapter = PROTOCOLS[protocol]
    except (KeyError, TypeError):
        raise ValueError('Choose chat_completions, anthropic, gemini, or responses protocol') from None
    return adapter(model, base_url, api_key)
