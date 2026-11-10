# Jackson — Renovate Builders' posting agent

Jackson turns job photos into posts and publishes them **himself** to **Instagram and Facebook**
through Meta's official API. There's no posting service to pay for.

1. On your phone, open Jackson, add job photos and a few words of notes.
2. Jackson looks at the photos and writes the Instagram caption and the Facebook post
   (English, Hebrew or both).
3. Edit anything, or tell him "shorter" / "mention the storage" and he rewrites it.
4. Tap **Approve & publish**, or schedule it. Nothing posts without your approval.

```
jackson-poster/
├── brand.md          ← EDIT: services, area, voice, hashtags
├── app.py            ← the agent
├── requirements.txt
├── index.html        ← the posts page
├── connect.html      ← the connect page
└── style.css
```

**Monthly cost: about $10.** Render server $7, Render disk $0.25, Claude about 5¢ per post.

---

# Setup from A to Z (about 1 hour, once)

You'll do this on a computer. After setup, you only use your phone.

## Step 1: Instagram Business account linked to your Facebook Page (5 min)

1. Instagram app → **Settings** → **Account type and tools** → **Switch to professional account** →
   **Business**.
2. Link it to your Facebook Page: on Facebook, open your **Renovate Builders Page** →
   **Settings** → **Linked accounts** → **Instagram** → **Connect account**, and log in to Instagram.

## Step 2: Business portfolio (5 min)

1. Go to **business.facebook.com**. If you don't have a business portfolio yet, create one
   called *Renovate Builders*.
2. Open **Settings** (gear icon) → **Accounts**:
   - **Pages** → make sure your Renovate Builders Page is listed (if not: *Add → Add a Page*).
   - **Instagram accounts** → make sure your Instagram is listed (if not: *Add*).

## Step 3: Create Jackson's Meta app and permanent access key (20 min)

**3a. Create the app**
1. Go to **developers.facebook.com** → log in → **My Apps** → **Create App**.
2. App name: `Jackson`. Contact email: your email.
3. Use case: choose **Other**, then app type **Business**.
4. Business portfolio: choose **Renovate Builders**. Click **Create app**.
5. In the app dashboard, add these two products / use cases (click **Set up** on each):
   - **Facebook Pages**: may be called *"Manage everything on your Page"*
   - **Instagram**: choose *"API setup with Facebook login"*
6. Leave the app in **Development** mode. You don't need Meta's app review, because Jackson only
   posts to your own business's accounts.

**3b. Create a "system user" (a robot account for Jackson)**
1. Back on **business.facebook.com** → **Settings** → **Users** → **System users** → **Add**.
2. Name: `Jackson`. Role: **Admin**. Click **Create system user**.
3. Select Jackson → **Assign assets**:
   - **Pages** → Renovate Builders → turn on **Full control** (or at least *Content*). Save.
   - **Instagram accounts** → your account → **Full control**. Save.
   - **Apps** → Jackson → **Full control** (Manage app). Save.

**3c. Generate the permanent key**
1. Still on the system user Jackson → **Generate token** (may be called *Generate new token*).
2. App: **Jackson**. Expiration: **Never**.
3. Tick these permissions:
   - `pages_show_list`
   - `pages_read_engagement`
   - `pages_manage_posts`
   - `instagram_basic`
   - `instagram_content_publish`
   - `business_management`
   - `ads_read` (only if it's in the list)
4. Click **Generate token** and **copy it** (a long text starting with `EAA…`).
   Keep it private, like a password. You'll paste it into Jackson in Step 6.

> If a permission isn't in the list, the matching product from 3a isn't added yet. Go back
> to the app dashboard, add it, then generate the token again.

## Step 4: Fill in your brand guide (10 min)

Open `brand.md` and replace every `[FILL IN]`: services, cities, website, phone, favorite hashtags.

## Step 5: Put Jackson online with Render (15 min)

**5a. GitHub**
1. Create a free account on **github.com** → **New repository** → name `jackson` → **Private** → Create.
2. Click **uploading an existing file** → drag in everything **inside** the `jackson-poster` folder
   (all 7 files: `app.py`, `brand.md`, `requirements.txt`, `README.md`, `index.html`, `connect.html`, `style.css`) → **Commit changes**.

**5b. Render**
1. Go to **render.com** → **Sign up with GitHub**.
2. **New +** → **Web Service** → choose your `jackson` repository.
3. Fill in:

| Setting | Value |
|---|---|
| Name | `jackson` |
| Region | **Frankfurt (EU Central)**, the closest to Israel |
| Language | **Python 3** |
| Build Command | `pip install -r requirements.txt` |
| Start Command | `uvicorn app:app --host 0.0.0.0 --port $PORT` |
| Instance Type | **Starter** ($7/month) |

4. **Environment Variables** → add:

| Key | Value |
|---|---|
| `ANTHROPIC_API_KEY` | your Claude key from console.anthropic.com (set a monthly limit there too) |
| `ADMIN_PASSWORD` | a strong password, which you'll type to open Jackson |
| `DATA_DIR` | `/data` |

5. **Advanced** → **Add Disk**: Name `jackson-data`, Mount Path `/data`, Size `1` GB.
   The disk keeps your scheduled posts and Meta connection safe when Render restarts.
6. **Advanced** → **Health Check Path**: `/health`.
7. Click **Deploy Web Service**. Wait until it shows **Live** (3–5 minutes).
8. Your address appears at the top, like `https://jackson-abcd.onrender.com`.

## Step 6: Connect Instagram and Facebook (2 min)

1. Open your Jackson address → log in with username **admin** and your `ADMIN_PASSWORD`.
2. Tap **Connect** (top right) → paste the key from Step 3c → **Connect**.
3. You should see: *Facebook Page: Renovate Builders · Instagram: @yourname · Access: never expires ✔*

## Step 7: First post and phone shortcut (5 min)

1. Tap **← Posts**, add 1–2 photos, write a short note, **Write the posts**.
2. For the first test, **untick Facebook** and publish to Instagram only. Tap **View on Instagram**
   to check it. Then try Facebook.
3. On your phone: open the Jackson address → browser menu → **Add to Home Screen**.
   Jackson now opens like an app.

You're done.

---

## Monthly cost

| Item | Cost |
|---|---|
| Render Starter server | $7.00 |
| Render disk (1 GB) | $0.25 |
| Claude Opus 5.5 | ~5¢ per post (≈ $1–3 for 20–40 posts) |
| Meta / Instagram / Facebook | free |
| **Total** | **≈ $10 per month** |

## Good to know

- **Photos:** JPG, PNG or iPhone HEIC. Jackson rotates them, converts to JPEG, and trims tall phone
  photos to Instagram's 4:5 shape (the center is kept).
- **Several photos** become an Instagram carousel and a Facebook multi-photo post. Instagram crops
  every photo in a carousel to the shape of the **first** one, so put your best shot first.
- **Scheduling:** Jackson publishes at the chosen time himself (Israel time). You can cancel a
  scheduled post before it goes out.
- **If one platform fails**, the other still publishes. Fix the problem and tap **Try again**:
  Jackson only retries what failed, so nothing is posted twice.
- **Limits:** Instagram allows up to 50 posts a day through the API, far more than you need.
- **Change voice/hashtags:** edit `brand.md` on GitHub. Render redeploys in about 2 minutes.
- **Google Business** can be added later, once Google approves API access.

## Quick alternative to Step 3b–3c (no business portfolio)

If you can't create a system user, use **Graph API Explorer** (developers.facebook.com/tools/explorer):
select the **Jackson** app → **Get User Access Token** with the same permissions → copy it.
Also add `META_APP_ID` and `META_APP_SECRET` in Render (from the app's **Settings → Basic**) so Jackson
turns it into a 60-day key. Facebook posting keeps working after that, but Instagram needs a new key
every 60 days. Jackson warns you 10 days before.

## Troubleshooting

| Message in Jackson | What to do |
|---|---|
| "This access key can't see any Facebook Page" | In 3b, assign the Page to the system user, then generate a new token |
| "no Instagram Business account linked to this Page" | Do Step 1 again: Business account and linked to the Page |
| "Permissions error" / "(#200)" / "(#10)" | A permission is missing from the token: generate it again with all permissions in 3c |
| "access key is expired or was revoked" | Generate a new token (3c) and paste it on the Connect page |
| "PUBLIC_URL isn't set" | Only off Render: add `PUBLIC_URL` = your public address |
| "Instagram couldn't process the photo" | Try another photo; very small or unusual images can fail |
| "Jackson couldn't write the post" | Check your Claude key and its spending limit |
| Scheduled posts vanished | The disk isn't mounted: check Step 5b-5 (`/data`) and `DATA_DIR=/data` |

Check `https://your-address/health`: it should show `"meta_connected": true` and `"data_dir": "/data"`.
