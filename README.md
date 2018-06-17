# Git Checks Buildkite Plugin 

A [Buildkite plugin](https://buildkite.com/docs/agent/v3/plugins) for uploading pipeline status via the Github [checks API](https://developer.github.com/v3/checks/).


## Example

The following pipeline will sleep in, and let you know it:

```yml
steps:
  - label: Teen Sleep
    command: sleep 60
    plugins:
      uw-ipd/github-checks#v0.0.2:
        output_title: Teen Sleep
        output_summary: "O, then I see Queen Mab hath been with you!"
```

## Setup

The plugin interacts with Github via application credentials, which are best
managed by [creating a private application](https://developer.github.com/apps/building-github-apps/creating-a-github-app/).
Feel free to direct webhooks to [`/dev/null`](https://devnull-as-a-service.com/dev/null).
The application *requires* read/write permissions for the checks API.

Make an application id and private-key available to your `buildkite-agent` by
whatever means necessary, remembering that a `private-key.pem` is serious
sekrat business. The `GITHUB_APP_AUTH_ID` and `GITHUB_APP_AUTH_KEY` environment
variables can be used to indicate a file containing the key/id (ideal) or
directly specify the id/key value (not so ideal) as env vars.

Now that you've gone to the trouble of registering an app, why not simplify
your private-repo access credentials? Check out
[git-credential-github-app-auth](https://github.com/uw-ipd/git-credential-github-app-auth).

The hook uses `docker-compose` to manage setup and execution.

## Configuration

### `output_title` (optional str)
### `output_summary` (optional path or str)
### `output_details` (optional path or str)

Specify check report output, as either inline strings *or* paths of build
products relative to the build root. If `output_title` is specified then
`output_summary` must be provided. Both the summary and details are rendered as
markdown.

### `app_id` (optional path or str)

Override the Github application id, otherwise defaulting to the agent
environment value `GITHUB_APP_AUTH_ID`.

### `private_key` (optional path or str)

Override the Github application private key, otherwise defaulting to the agent
environment value `GITHUB_APP_AUTH_KEY`.

### `debug` (optional boolean)

Enable debug-level logging of plugin actions.

## License

MIT (see [LICENSE](LICENSE))
