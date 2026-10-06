# API keys

Each Claude call of a run uses a model provider. The provider gets the prompt and the code that the agent reads. You can add your own API key for a provider. Then your key pays for the calls on your repositories.

## Who can add keys

You can add keys only for your own GitHub account. The keys apply to all repositories of that account that have Rookery installed.

You cannot add keys for an organization. The operator of Rookery sets the provider for an organization.

## Providers

| Provider | Key from | Models |
|---|---|---|
| Anthropic (Claude) | console.anthropic.com | Claude: the models that Claude Code uses |
| DeepSeek | platform.deepseek.com | DeepSeek models in place of Claude |
| Z.ai (GLM) | z.ai | GLM models in place of Claude |

The provider of the selected key gets your code. Read the data policy of that provider before you add a key.

## Add a key

1. In the portal, select **Keys**.
2. Select the provider.
3. Type a label. The label is optional, and it can have a maximum of 40 characters.
4. Paste the key.
5. Select **Save key**.

If you have no selected key, Rookery selects the new key. Rookery does not check the key when you save it. If the key is not correct, the first run fails with the error of the provider.

## Select a key

You can keep more than one key. Only the selected key is used. To change it, select a different key in the list. The change applies from the next Claude call, also in a running job.

**Rookery's default** is the provider that the operator selects for your account. If the operator selected no provider for your account, you cannot select **Rookery's default**.

## Remove a key

Select **Remove** next to the key. If it is the selected key, the runs use **Rookery's default**. If there is no default for your account, the runs fail until you select a different key.

## No key

If your account has no selected key and no default, a run does not start. Rookery writes a comment with a link to the **Keys** page and sets `rookery:error`. No Claude call occurs, and the run does not count in the daily limits.

To start the run again after you add a key:

- On an issue: remove the `rookery:error` and `rookery:trigger` labels. Then add `rookery:trigger` again. If you approved the plan before the error, comment `approve` again.
- On a pull request: add `rookery:pr-review` again, or mention `@rookery-agent` again.

## How Rookery keeps the key

- Rookery encrypts the key before it writes it to its database. The operator's master key is necessary to read it.
- Rookery never shows the key again. The portal shows only the provider, the label and the last 4 characters.
- Rookery uses the key only for the Claude calls on your repositories.
- The daily limits of each repository also apply to calls on your own key.

During a call, the key is in the environment of the Claude process on the Rookery server. The agents of all repositories run as the same user on that server. Thus an agent that runs at the same time could read the key. Use a key that you can revoke, and set a spending limit for it at the provider.
