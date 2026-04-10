# NBA Halftime 4 AST / 4 REB Discord Bot Setup

This bot posts to a Discord channel when a player has **exactly 4 assists and 4 rebounds by halftime**.

## What you need
- A Discord server where you are an admin
- A Discord bot token
- The Discord channel ID where alerts should post
- A BALLDONTLIE API key with NBA box score access
- A simple host such as Railway

## 1) Create the Discord bot
1. Open the Discord Developer Portal.
2. Click **New Application**.
3. Name it anything you want.
4. Open the **Bot** tab.
5. Click **Add Bot**.
6. Under **Privileged Gateway Intents**, turn on **Message Content Intent**.
7. Copy the bot token. Save it.

## 2) Invite the bot to your server
1. In the same application, open **OAuth2** -> **URL Generator**.
2. Under **Scopes**, check **bot**.
3. Under **Bot Permissions**, check:
   - **Send Messages**
   - **View Channels**
   - **Read Message History**
4. Copy the generated URL.
5. Open it and invite the bot to your server.

## 3) Get your channel ID
1. In Discord, go to **User Settings** -> **Advanced**.
2. Turn on **Developer Mode**.
3. Right-click the channel where alerts should go.
4. Click **Copy Channel ID**.

## 4) Get a BALLDONTLIE API key
1. Create an account at BALLDONTLIE.
2. Generate an API key.
3. Make sure your plan includes **NBA live box scores**.

## 5) Deploy on Railway
1. Create a GitHub repository.
2. Upload these files to the repo root:
   - `nba_44_halftime_discord_bot.py`
   - `requirements_nba_44_bot.txt`
3. In Railway, create a new project from that GitHub repo.
4. Set the **Start Command** to:

```bash
python nba_44_halftime_discord_bot.py
```

5. Add these environment variables in Railway:

```text
DISCORD_BOT_TOKEN=your_discord_bot_token
DISCORD_CHANNEL_ID=your_channel_id
BALLDONTLIE_API_KEY=your_api_key
POLL_SECONDS=45
EXACT_ONLY=true
```

Optional team filter:

```text
TEAM_FILTER=LAL,BOS,NYK
```

## Notes
- `EXACT_ONLY=true` means exactly 4 assists and 4 rebounds.
- `EXACT_ONLY=false` means 4 or more assists and 4 or more rebounds.
- The bot checks every 45 seconds by default.
- If the bot is in the server but not posting, make sure it can see and send messages in the selected channel.

## Troubleshooting
- **Bot shows offline:** the host is not running the script or the token is wrong.
- **No alerts:** verify the API plan includes NBA live box scores and the channel ID is correct.
- **Permission error in Discord:** re-invite the bot with Send Messages and View Channels permissions.
