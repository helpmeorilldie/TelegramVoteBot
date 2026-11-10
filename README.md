# Giveaway Vote Bot (V3)

Same look as your tournament bot (small-caps font, blockquote headers, coloured buttons, premium emoji,
self-cleaning panels), built on aiogram 3.25+.

## How it works
1. **Admin** opens `/admin` → **New Giveaway** → sends a name and a prize → gets a link
   `https://t.me/<bot>?start=gw_xxxx` (with Share and QR buttons).
2. **User** opens that link → (joins the channel if needed) → sends their name →
   the name is registered **for that giveaway** and posted in your channel with a **Vote (N)** button.
3. **Voting rules** (all enforced in one place, same for both vote buttons):
   - voter must be in the channel (checked live at the moment of voting)
   - voter must have **started the bot**
   - **one vote per person per giveaway**
   - banned users / closed giveaways can't vote
4. **Voter hasn't started the bot?** Tapping Vote in the channel opens the bot directly on that entry
   with a **"Vote For <name>"** button. One tap there and the vote counts, and the channel button updates.
   Voters who already started the bot vote straight from the channel and get a confirmation in the bot.
5. **Leaderboard** inside the bot: pick a giveaway, see top 15 with 🥇🥈🥉, your own row marked, and a
   Refresh button. Users also get **My Entries** (their rank and votes) and Share / View Post buttons.

## Admin panel
- New Giveaway, Manage Giveaways (share link, QR code, leaderboard, close/reopen,
  top-voted or random winner, announce winner in channel, delete entries, delete giveaway)
- Post Update, Broadcast, Statistics, Users, `/ban` `/unban`, Export CSV
- **Backup Now** and **Restore Backup** (restore the pinned latest backup or upload a `.db` file)
- Everything is backed up to the **private storage channel**; the latest backup is pinned. If the
  server's disk is wiped, the bot restores itself from that pin on start-up.
  Backups run every `BACKUP_EVERY_HOURS` (if something changed) and after every `BACKUP_EVERY_CHANGES` changes.

## Setup
1. Create the bot in @BotFather.
2. Add the bot as **admin** in your voting channel (post + delete messages; needed to check members).
3. Create a **private** channel, add the bot as admin there (post + pin messages), and get its `-100…` id.
4. `pip install -r requirements.txt`
5. Copy `env.example` to `.env`, fill it in.
6. `python botV3.py`

Premium emoji / coloured button icons are tested at start-up and fall back to normal emoji if Telegram
doesn't allow them for your bot. You'll get a short report in your DM.

## Notes
- Membership is checked when the vote is tapped; someone who votes then leaves keeps their vote.
- The "start the bot first" check uses people who have opened the bot at least once.
- Deleting an entry also frees that entry's voters to vote again.
