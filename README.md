# 🤫 SecretShare-Bot

**SecretShare-Bot** is a privacy-first Telegram bot built using [PyroFork](https://github.com/pyrogram/pyrofork). It allows users to share secrets and covert messages through Telegram—safely and anonymously. The bot supports premium user management, inline query sharing, MongoDB integration, and reaction-based control.

---

## ✨ Features

- 🔐 Anonymous secret sharing
- 💼 Premium user & sudo access management
- ⚡ Inline query support
- 📦 MongoDB-based persistent storage
- 🔄 Reaction-based controls
- 🆙 Optional ping to prevent Render.com timeout

---

## 🌐 Deployment (Render Manual Setup)

### 🪜 Step-by-Step Instructions

#### 1. **Fork the Repository**

Click the **Fork** button in the top-right corner of this GitHub repo and fork it to your account.

#### 2. **Go to Render.com**

- Visit [https://render.com](https://render.com) and log in.
- Click **"New Web Service"**
- Connect your GitHub and select the forked repository.

#### 3. **Configure Render Web Service**

- **Environment:** `Python`
- **Build Command:**

```bash
pip install -r requirements.txt
````

* **Start Command:**

```bash
gunicorn app:app & python3 main.py & python3 ping.py
```

#### 4. **Add Environment Variables**

In the "Environment" tab, add the following variables one by one:

| Key             | Value                             |
| --------------- | --------------------------------- |
| `API_ID`        | Your Telegram API ID              |
| `API_HASH`      | Your Telegram API Hash            |
| `BOT_TOKEN`     | Your Bot token from @BotFather    |
| `MONGO_URI`     | Your MongoDB connection URI       |
| `OWNER_ID`      | Your Telegram numeric user ID     |
| `BOT_USERNAME`  | Your bot's username (without `@`) |
| `PING_URL`      | Your Render web service URL       |
| `PING_INTERVAL` | (Optional) Seconds, default: `20` |

> Example `PING_URL`: `https://secretshare-bot.onrender.com`

> ⚠️ You must deploy as a **Web Service**, not as a background worker.

---

## 💻 Run Locally (For Testing)

### 1. **Clone the Repo**

```bash
git clone https://github.com/ByteSupreme/SecretShare-Bot
cd SecretShare-Bot
```

### 2. **Install Requirements**

```bash
pip install -r requirements.txt
```

### 3. **Create `.env` File**

Create a `.env` file in the root directory:

```env
API_ID=your_api_id
API_HASH=your_api_hash
BOT_TOKEN=your_bot_token
MONGO_URI=your_mongo_uri
OWNER_ID=your_owner_id
BOT_USERNAME=your_bot_username
PING_URL=https://your-render-url (optional)
PING_INTERVAL=20
```

### 4. **Start the Bot**

```bash
python3 main.py
```

---

## 🧠 Powered By

* [PyroFork](https://github.com/pyrogram/pyrofork)
* [Pyrogram](https://github.com/pyrogram/pyrogram)
* [MongoDB](https://www.mongodb.com/)
* ❤️ Open Source Community

---

## 👤 Maintainer

* [ByteSupreme](https://github.com/ByteSupreme)

---

## 📄 License

This project is licensed under the [MIT License](LICENSE).

---

## 💬 Support & Contributions

* Open an [Issue](https://github.com/ByteSupreme/SecretShare-Bot/issues) for bug reports or help.
* Submit a [Pull Request](https://github.com/ByteSupreme/SecretShare-Bot/pulls) to contribute.
