# Self-hosting

You can run your own Rookery server with your own GitHub App. This page gives the main steps. The full guide for a Linux server is in [DEPLOY.md](https://github.com/sa-es-ir/rookery/blob/main/DEPLOY.md).

## Requirements

| Tool | Purpose |
| --- | --- |
| Python 3.10 or later | Runs Rookery. No packages are necessary. |
| `git`, with `user.name` and `user.email` set | Rookery makes the commits. |
| `gh` | Reads issues, writes comments and opens pull requests. |
| `claude` | Writes the plan and the code. |
| `openssl` | Signs the token of the GitHub App. |

## 1. Make a GitHub App

Make a GitHub App in your GitHub settings. Set the webhook URL to your server and the content type to JSON.

The permissions and events are in step 0 of [DEPLOY.md](https://github.com/sa-es-ir/rookery/blob/main/DEPLOY.md). Make a private key and a client secret for the App.

## 2. Write the .env file

Make a `.env` file next to `agent.py`:

| Variable | Value |
| --- | --- |
| `WEBHOOK_SECRET` | The webhook secret of the App. |
| `APP_ID` | The ID of the App. |
| `APP_KEY` | The path to the private key. |
| `CLIENT_ID`, `CLIENT_SECRET` | The OAuth client of the App, for the portal. |
| `PUBLIC_URL` | The URL of your server, for example `https://rookery.example.com`. |
| `PORT` | The port. The default is 8787. |
| `RUN_CAP`, `USD_CAP` | Optional. The daily limits. The defaults are 30 and 20. |
| `REPOS_ONLY` | Optional. Rookery works only on these repositories, for example `owner/a,owner/b`. |

Set the callback URL of the App to `PUBLIC_URL/auth/callback`. Set the Setup URL to `PUBLIC_URL/auth/login`. Do not select *Request user authorization (OAuth) during installation*. If you select it, sign-in fails.

## 3. Test without a webhook

These commands use the same code as the webhook:

```bash
python agent.py owner/repo 12              # plan issue 12
python agent.py owner/repo 12 approve      # do the tasks and open the pull request
python agent.py owner/repo 12 use sqlite   # send a reply to the plan
python test_agent.py                       # run the tests
```

## 4. Start the server

```bash
python agent.py
```

The server listens on `127.0.0.1:$PORT`. For a test on your computer, send the webhooks through smee:

```bash
npx smee-client --url https://smee.io/<channel> --target http://127.0.0.1:8787/
```

On a public server, put a reverse proxy with TLS in front of Rookery. Set `PUBLIC_URL` to the `https://` address.

## 5. Install the App

Install the App on your repositories. Rookery clones each repository and creates the labels.

## Operation

- To stop all new Claude calls, create a file with the name `PAUSED` next to `agent.py`. Delete the file to continue.
- The daily usage is in `$WORKSPACE/rookery.db`.
- A restart marks the running jobs as `interrupted` in the run log.
- Do not run `gh auth login` on the server. The agent can read a stored login token.
