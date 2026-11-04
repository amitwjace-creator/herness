"""Command-line entry points."""

import argparse
import json
import os
import sys
from pathlib import Path

from .core import Agent, ModelError, OpenAIClient, Session
from .tools import ToolRunner


class DemoClient:
    """An explicitly scripted tool demonstration; no model or network involved."""

    def complete(self, messages):
        last = messages[-1]
        if last["role"] == "tool":
            result = json.loads(last["content"])
            entries = result.get("entries", [])
            return {"role": "assistant", "content": (
                "Offline demo complete. I used the real list_files tool and found:\n\n"
                + ("\n".join(entries) or "An empty folder.")
                + "\n\nConnect a model in your server environment to enable real conversations. "
                "This demo uses scripted responses, not AI."
            )}
        return {"role": "assistant", "content": "I’ll inspect your project folder using a real tool.",
                "tool_calls": [{"id": "demo-list", "type": "function", "function": {
                    "name": "list_files", "arguments": '{"path":"."}'}}]}


    def stream(self, messages, on_delta, cancel):
        message = self.complete(messages)
        for word in (message.get('content') or '').split(' '):
            cancel.check()
            on_delta(word + ' ')
            cancel.event.wait(.025)
        cancel.check()
        return message


def model_client(demo=False, require_key=True):
    if demo:
        return DemoClient()
    protocol = os.environ.get('HERNESS_PROTOCOL', 'chat_completions')
    defaults = {'anthropic': ('https://api.anthropic.com/v1', 'claude-sonnet-4-5', 'ANTHROPIC_API_KEY'),
                'gemini': ('https://generativelanguage.googleapis.com/v1beta', 'gemini-2.5-flash', 'GEMINI_API_KEY')}
    base_default, model_default, key_name = defaults.get(protocol, ('https://api.openai.com/v1', 'gpt-4.1-mini', 'OPENAI_API_KEY'))
    base = os.environ.get('HERNESS_API_BASE', base_default)
    model = os.environ.get('HERNESS_MODEL', model_default)
    key = os.environ.get('HERNESS_API_KEY') or os.environ.get(key_name, '')
    if require_key and not key and base.rstrip('/') == base_default:
        raise ValueError("Set HERNESS_API_KEY (or OPENAI_API_KEY) in the server environment, or use --demo.")
    from .providers import create_client
    return create_client(protocol, model, base, key)


def approve(description):
    print("\n" + (description.get("description", "") + "\n" + description.get("diff", "") if isinstance(description, dict) else description))
    try:
        return input("Allow this action? [y/N] ").strip().lower() == "y"
    except EOFError:
        return False


def main(argv=None):
    parser = argparse.ArgumentParser(description="Herness — your personal agent workspace")
    parser.add_argument("--workspace", default=".", help="Project folder the agent works in")
    sub = parser.add_subparsers(dest="mode", required=True)
    web = sub.add_parser("web", help="Start the local web app")
    web.add_argument("--port", type=int, default=8765)
    web.add_argument("--demo", action="store_true", help="Scripted offline tool demonstration")
    mobile = sub.add_parser('mobile', help='Run the authenticated native API behind your private HTTPS proxy')
    mobile.add_argument('--public-origin', required=True, help='HTTPS origin served by your private proxy')
    mobile.add_argument('--key-file', required=True, help='Private mobile bearer key file created with mobile-key')
    mobile.add_argument('--port', type=int, default=8766, help='Loopback API port (default: 8766)')
    mobile.add_argument('--demo', action='store_true', help='Scripted offline tool demonstration')
    mobile_key = sub.add_parser('mobile-key', help='Create a private native-client key file without overwriting anything')
    mobile_key.add_argument('--output', required=True, help='New key file path; parent directory must already exist')
    chat = sub.add_parser("chat", help="Chat in the terminal")
    chat.add_argument("prompt", nargs="?")
    chat.add_argument("--session", default="default")
    chat.add_argument("--demo", action="store_true")
    chat.add_argument('--plan', action='store_true', help='Explore and plan using read-only tools')
    chat.add_argument('--max-steps', type=int, default=12, choices=range(1, 101), metavar='1–100')
    sub.add_parser("doctor", help="Check configuration without contacting a model")
    args = parser.parse_args(argv)
    root = Path(args.workspace).resolve()
    try:
        if args.mode == 'mobile-key':
            from .mobile import generate_mobile_key
            output = generate_mobile_key(args.output)
            print(f'Created private mobile key file: {output}')
            print('Transfer the key privately to your device. The key was not printed.')
            return 0
        if not root.is_dir():
            raise ValueError("Workspace must be an existing directory")
        if args.mode == "doctor":
            print(f"Workspace: {root}\nPython: {sys.version.split()[0]}")
            print("API key: " + ("present" if os.environ.get("HERNESS_API_KEY") or os.environ.get("OPENAI_API_KEY") else "absent"))
            model_client()
            print("Configuration valid. Live model connectivity has not been tested.")
            return 0
        client = model_client(args.demo, require_key=args.mode not in {'web', 'mobile'})
        if args.mode == 'mobile':
            from .mobile import serve_mobile
            serve_mobile(root, client, args.public_origin, args.key_file, args.port, args.demo)
            return 0
        if args.mode == "web":
            from .web import serve
            serve(root, client, args.port, args.demo)
            return 0
        from .library import Project
        from .privacy import Privacy
        mode = 'plan' if args.plan else 'act'
        agent = Agent(client, ToolRunner(root, approve, mode=mode, scopes=Privacy(root).tool_scopes()), Session(root, args.session),
                      max_steps=args.max_steps, mode=mode, project=Project(root).read())
        if args.prompt:
            agent.run(args.prompt)
        else:
            print("Herness · type /exit to leave. Conversations are saved locally.")
            while True:
                try:
                    prompt = input("\nyou › ").strip()
                except EOFError:
                    break
                if prompt == "/exit":
                    break
                if prompt:
                    agent.run(prompt)
        return 0
    except (ModelError, ValueError, OSError) as exc:
        print(f"Herness: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nStopped.")
        return 130
