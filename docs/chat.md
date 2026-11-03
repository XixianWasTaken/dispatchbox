# Slack and Feishu

dispatchbox works with Slack, Feishu (飞书), or both at once. With a chat platform connected you can:

- **Start runs from chat.** @mention the bot in a channel or send it a DM. The bot replies with the run number.
- **Decide with buttons.** A decision card goes back to the conversation the request came from. Runs started by GitHub, cron or HTTP post their cards to a notify channel instead.
- **Get the answer in place.** When a run started from chat finishes, the agent's answer is posted in the same thread, including after a decision. The `DISPATCHBOX_*` marker lines are stripped, and long answers are cut (3,000 characters on Slack, 2,000 on Feishu).
- **Keep one platform per conversation.** A run started in Feishu is answered only in Feishu, and the same goes for Slack, so there is never a second set of buttons elsewhere.

Both platforms push requests to the hub, so the hub needs a public HTTPS address. Pass it as `--public-url` so the links in cards point to your dashboard. For a quick test, a tunnel such as `cloudflared tunnel --url http://127.0.0.1:8787` works.

- [Rules for chat](#rules-for-chat)
- [Settings](#settings)
- [Slack setup](#slack-setup)
- [Feishu setup](#feishu-setup)
- [Plain webhooks](#plain-webhooks)

## Rules for chat

A chat message becomes an event like this one:

```json
{
  "source": "slack",
  "type": "message.mention",
  "subject": "<channel or chat id>",
  "summary": "<first 200 characters of the message>",
  "payload": { "chat": "...", "thread": "...", "user": "<user id>", "text": "<the full message>" }
}
```

`type` is `message.dm` for direct messages, and `source` is `feishu` for Feishu. Use `{payload.text}` in the prompt when messages may be long. Match it with a rule:

```json
{
  "name": "chat-request",
  "when": { "source": ["slack", "feishu"], "types": ["message.mention", "message.dm"] },
  "cwd": "~/code/myapp",
  "prompt": "A teammate asked in chat: {payload.text}. Do it in this repository and reply with a short answer.",
  "args": ["--allowed-tools", "Read,Glob,Grep"],
  "group": "chat:{subject}"
}
```

Decisions made from a card resume the agent session like any other decision. See [Decisions](rules.md#decisions).

## Settings

Who may do what lives in the `chat` section of the rules file. Changes apply to the next message.

```json
"chat": {
  "slack":  { "notify_channel": "C0123", "allowed_users": ["U0123"], "approvers": ["U0123"] },
  "feishu": { "notify_to": "oc_xxx", "notify_to_type": "chat_id", "allowed_users": ["ou_xxx"], "approvers": ["ou_xxx"] }
}
```

| Key | Default | What it does |
|---|---|---|
| `allowed_users` | `[]` | User IDs that may start runs. An empty list allows nobody; `["*"]` allows everyone in the workspace. Someone not on the list gets a reply with their user ID, so you can add them. |
| `approvers` | `allowed_users` | User IDs that may click decision buttons. |
| `notify_channel` (Slack) | none | Channel ID for cards and notifications of runs that did not start in Slack. |
| `notify_to` (Feishu) | none | Where Feishu cards and notifications go for runs that did not start in Feishu. |
| `notify_to_type` (Feishu) | `chat_id` | `chat_id` for a group, `open_id` for a person. |
| `ack` | `true` | Reply "Got it. Queued as run #N." to each request. |
| `reply_on_finish` | `true` | Post the result in the original conversation. |

A card shows at most five options.

## Slack setup

1. Create an app at [api.slack.com/apps](https://api.slack.com/apps).
2. **OAuth & Permissions**: add the bot scopes `chat:write`, `app_mentions:read` and `im:history`, then install the app. Copy the **Bot User OAuth Token** (`xoxb-...`).
3. **Basic Information → App Credentials**: copy the **Signing Secret**.
4. Start the hub with both values, so it can answer Slack's URL check:

   ```bash
   export DISPATCHBOX_SLACK_BOT_TOKEN=xoxb-xxx
   export DISPATCHBOX_SLACK_SIGNING_SECRET=xxx
   python3 -m dispatchbox hub --rules rules.json --public-url https://<your hub>
   ```

   The hub log should say `chat: slack`.
5. **Event Subscriptions**: turn events on, set the Request URL to `https://<your hub>/hooks/slack`, wait for **Verified**, and subscribe to the bot events `app_mention` and `message.im`.
6. **Interactivity & Shortcuts**: turn it on with the same URL.
7. **App Home**: turn on the Messages tab, so people can DM the bot.
8. Invite the bot to the channels where it should listen or post.

dispatchbox checks Slack's signature on every request and rejects requests whose timestamp is more than five minutes off.

## Feishu setup

1. At [open.feishu.cn/app](https://open.feishu.cn/app), create a self-built app (企业自建应用) and add the **Bot** feature. A custom group bot cannot receive button clicks, so it will not work.
2. **Permissions**: add `im:message:send_as_bot`, `im:message.p2p_msg:readonly` and `im:message.group_at_msg:readonly`.
3. **Credentials & Basic Info**: copy the App ID and App Secret. **Events & Callbacks → Encryption Strategy**: copy the **Verification Token**. It sits next to the Encrypt Key and the two look alike, so check you copied the right one.
4. Start the hub with these values:

   ```bash
   export DISPATCHBOX_FEISHU_APP_ID=cli_xxx
   export DISPATCHBOX_FEISHU_APP_SECRET=xxx
   export DISPATCHBOX_FEISHU_VERIFICATION_TOKEN=xxx
   python3 -m dispatchbox hub --rules rules.json --public-url https://<your hub>
   ```

   The hub log should say `chat: feishu`.
5. **Events & Callbacks → Event Configuration**: choose sending events to a developer server, set the URL to `https://<your hub>/hooks/feishu`, and add `im.message.receive_v1`.
6. **Callback Configuration**: same URL, add `card.action.trigger`.
7. **Version Management & Release**: publish a version. Feishu applies events, callbacks and permissions only after that. Include everyone who will message the bot in the availability scope, and add the bot to the group you use as `notify_to`.

If you set an Encrypt Key in the Feishu console, also set `DISPATCHBOX_FEISHU_ENCRYPT_KEY` and install the `cryptography` package. dispatchbox then decrypts every request and checks its signature. Without an Encrypt Key, it checks the Verification Token.

## Plain webhooks

To be notified without a chat app, set `DISPATCHBOX_NOTIFY_URL` to a Slack incoming webhook, a Feishu custom bot, or any URL that accepts a JSON POST. These messages link to the dashboard but carry no buttons. Decide on the dashboard instead.
