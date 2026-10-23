# Repo notes

Rookery keeps short notes for each repository. The agents read the notes at the start of each run. Thus a run does not have to find the same commands, files and problems again.

## What the notes contain

- Where things are in the repository.
- The commands that build and test the repository, and if they work.
- The conventions of the repository.
- Traps: commands that failed, and why.

The notes do not contain data about one issue or one pull request. The maximum size is 8,000 characters.

## Where the notes come from

1. A plan, reply, implementation or pull request review finishes. It can finish with or without an error. A run that you cancel does not change the notes.
2. Rookery makes a short record of the run: the tools that the agent used and the errors that it got. The record does not contain file contents.
3. A small model (haiku) reads the current notes and the record, and writes the updated notes.
4. Rookery checks the reply. If the reply is not correct, Rookery keeps the old notes.
5. If the notes are longer than 8,000 characters, the model writes them again, shorter. This is one more Claude call. If the notes are still too long, Rookery removes the lines after the limit. The length of the notes does not cause an error.
6. Rookery writes the notes.

This step is one more Claude call. It counts in the daily limits of the repository. If it fails, Rookery does not write a comment and does not set a label. The run log in the portal shows it as a "repo notes" run.

## Where the notes are

The notes are on the Rookery server. Rookery does not write them to your repository. When you remove a repository from the App, Rookery deletes its notes.

## How the agent uses the notes

Rookery reads the notes one time at the start of each job and gives them to each agent in the job. Each agent gets this rule with the notes: the code and `CLAUDE.md` win when the notes disagree with them.

The run log shows the size of the notes that a job read, on the step that reads `CLAUDE.md`.

If `CLAUDE.md`, the notes and the instructions are more than 64 KB together, Rookery does not give the notes to that job. The job continues.

## See and clear the notes

In the portal, open a repository and select **Workflow**. The **Repo notes** card shows the notes and the run that wrote them.

To delete the notes, select **Clear notes**. You must have write access to the repository. The next run starts with no notes.

You cannot edit the notes in the portal.

## Turn the notes off

The operator can set `MEMORY=0` in `.env`. Then no run reads or writes notes. This setting is for all repositories on that Rookery server.
