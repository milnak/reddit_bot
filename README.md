# RedditBot

A Reddit bot that reads reply rules from JSON and responds when a post matches one of those regular expressions.

## Setup

### 1. Create a Reddit app

1. Log in as your bot account at <https://www.reddit.com/prefs/apps>
2. Click **"are you a developer? create an app…"**
3. Choose **script**, give it any name, set redirect URI to `http://localhost:8080`
4. Note the **client ID** (under the app name) and **client secret**

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure credentials

```bash
cp .env.example .env
# Edit .env with your actual credentials
```

### 4. Configure reply rules

Create a `reddit_bot.json` file in the project root. It should contain a JSON array of objects with `pattern` and `reply` fields. The bot checks the rules in order and uses the first match.

Example:

```json
[
   {
      "pattern": "\\bGeorge Washington\\b",
      "reply": "Hi George!"
   }
]
```

### 5. Run the bot

```bash
python reddit_bot.py
```
