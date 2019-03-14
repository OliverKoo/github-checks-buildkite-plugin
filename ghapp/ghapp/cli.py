import logging
import json
import os
from typing import Optional
import requests

import cattr

import aiohttp
import aiorun
import asyncio
import click
from decorator import decorator

from .github.identity import AppIdentity
from .github import checks
from .github.gitcredentials import credential_helper

from .buildkite import jobs
from .handlers import RepoName, job_environ_to_check_action

logger = logging.getLogger(__name__)

#https://developer.github.com/apps/building-github-apps/authenticating-with-github-apps/#authenticating-as-a-github-app
pass_appidentity = click.make_pass_decorator(AppIdentity, ensure=True)


@decorator
def aiomain(coro, *args, **kwargs):
    aiorun.logger.setLevel(51)

    async def main():
        try:
            await coro(*args, **kwargs)
        finally:
            asyncio.get_event_loop().stop()

    return aiorun.run(main())


@click.group()
@click.option(
    '--app_id',
    help=("Integer app id, or path to file containing id. "
          "Resolved from $%s." % AppIdentity.APP_ID_ENV_VAR),
    envvar=AppIdentity.APP_ID_ENV_VAR,
)
@click.option(
    '--private_key',
    help=("App private key, or path to private key file. "
          "Resolved from $%s." % AppIdentity.PRIVATE_KEY_ENV_VAR),
    envvar=AppIdentity.PRIVATE_KEY_ENV_VAR,
)
@click.option(
    '-v',
    '--verbose',
    count=True,
    help="'-v' for logging, '-vv' for debug logging. "
    "Resolved via $GITHUB_APP_AUTH_DEBUG ('1' or '2').",
    envvar="GITHUB_APP_AUTH_DEBUG",
)
@click.pass_context
def main(ctx, app_id, private_key, verbose):
    if verbose:
        logging.basicConfig(
            level=logging.INFO if verbose == 1 else logging.DEBUG,
            format="%(name)s %(message)s",
        )

    ctx.obj = AppIdentity(app_id=app_id, private_key=private_key)


@main.add_command
@click.command(help="Resolve app id/key and check app authentication.")
@pass_appidentity
@aiomain
async def current(appidentity: AppIdentity):
    async with aiohttp.ClientSession(
            headers=appidentity.app_headers(), ) as session:
        async with session.get('https://api.github.com/app', ) as resp:
            resp.raise_for_status()
            print(json.dumps(await resp.json(), indent=2))


@main.add_command
@click.command(help="Generate access token for installation.")
@pass_appidentity
@click.argument('account')
@aiomain
async def token(appidentity, account):
    print(await appidentity.installation_token_for(account))


@main.group(help="git-credential helper implementation.")
def credential():
    pass


@credential.add_command
@click.command(help="Credential storage helper implementation.")
@pass_appidentity
@click.argument('input', type=click.File('r'), default="-")
@click.argument('output', type=click.File('w'), default="-")
@aiomain
async def get(appidentity, input, output):
    # https://git-scm.com/docs/git-credential
    logger.debug("get id: %s input: %s output: %s", appidentity, input, output)
    output.write(await credential_helper(input.read(),
                                         appidentity.installation_token_for))
    output.write("\n")


@credential.command(help="no-op git-credential interface")
def store():
    pass


@credential.command(help="no-op git-credential interface")
def erase():
    pass


@main.group(help="github checks api support")
def check():
    pass


@check.add_command
@click.command()
@pass_appidentity
@click.argument('repo', type=str)
@click.argument('ref', type=str)
@aiomain
async def list(app: AppIdentity, repo: str, ref: str):
    """List current checks on given repo ref."""
    repo = RepoName.parse(repo)

    async with aiohttp.ClientSession(
            headers=await app.installation_headers(repo.owner)) as sesh:
        fetch = checks.GetRuns(owner=repo.owner, repo=repo.repo, ref=ref)
        print(await fetch.execute(sesh))


@check.add_command
@click.command()
@pass_appidentity
@click.argument('repo', type=str)
@click.argument('branch', type=str)
@click.argument('name', type=str)
@click.option('--sha', type=str, default=None)
@click.option('--output_title', type=str, default=None)
@click.option('--output_summary', type=str, default=None)
@click.option('--output', type=str, default=None)
@aiomain
async def push(
        app: AppIdentity,
        repo: str,
        branch: str,
        sha: str,
        name: str,
        output_title: str,
        output_summary: Optional[str],
        output: Optional[str],
):
    """Push a check to github."""
    repo = RepoName.parse(repo)
    output = load_job_output(output_title, output_summary, output)

    async with aiohttp.ClientSession(
            headers=await app.installation_headers(repo.owner)) as sesh:

        if not sha:
            logging.info("Resolving branch sha: %s", branch)
            ref_url = (
                f"https://api.github.com"
                f"/repos/{repo.owner}/{repo.repo}/git/refs/heads/{branch}"
            )
            logging.debug(ref_url)
            resp = await sesh.get(ref_url)
            logging.info(resp)
            sha = (await resp.json())["object"]["sha"]

        action = checks.CreateRun(
            owner=repo.owner,
            repo=repo.repo,
            run=checks.RunDetails(
                head_branch=branch,
                head_sha=sha,
                name=name,
                status=checks.Status.in_progress,
                output = output,
            ))

        async with action.execute(sesh) as resp:
            logging.debug(resp)

            try:
                resp.raise_for_status()
            except Exception:
                logging.exception((await resp.json())["message"])
                raise

            print(await resp.json())


@check.add_command
@click.command()
@pass_appidentity
@click.argument('repo', type=str)
@click.argument('id', type=str)
@click.argument('name', type=str)
@aiomain
async def update(
        app: AppIdentity,
        repo: str,
        id: str,
        name: str,
):
    """List current checks on given repo ref."""
    repo = RepoName.parse(repo)

    action = checks.UpdateRun(
        owner=repo.owner,
        repo=repo.repo,
        run=checks.RunDetails(
            id=id,
            name=name,
            status=checks.Status.in_progress,
        ))

    async with aiohttp.ClientSession(
            headers=await app.installation_headers(repo.owner)) as sesh:

        async with action.execute(sesh) as resp:
            logging.debug(resp)

            try:
                resp.raise_for_status()
            except Exception:
                logging.exception((await resp.json())["message"])
                raise

            print(await resp.json())


@check.add_command
@click.command()
@pass_appidentity
@click.option('--output_title', type=str, default=None)
@click.option('--output_summary', type=str, default=None)
@click.option('--output', type=str, default=None)
@aiomain
async def from_job_env(
    app: AppIdentity,
    output_title: str,
    output_summary: Optional[str],
    output: Optional[str],
):
    job_env = cattr.structure(dict(os.environ), jobs.JobEnviron)
    logging.info("job_env: %s", job_env)

    repo = RepoName.parse(job_env.BUILDKITE_REPO)

    async with aiohttp.ClientSession(
            headers=await app.installation_headers(repo.owner)) as sesh:

        current_runs = await checks.GetRuns(
            owner=repo.owner,
            repo=repo.repo,
            ref=job_env.BUILDKITE_COMMIT,
        ).execute(sesh)
        logging.info("current_runs: %s", current_runs)

        check_action = job_environ_to_check_action(job_env, current_runs)
        # output = load_job_output(output_title, output_summary, output)
        # if output:
        #     check_action.run.output = output
        output = get_buildkite_output()

        logging.info("action: %s", check_action)


        await check_action.execute(sesh)

# def load_job_output(output_title, output_summary, output):
#     """Loads job output (maybe) from files, to be moved to handler layer."""
#     def read_if_file(val):
#         if os.path.exists(val):
#             logger.info("Reading file: %s", val)
#             with open(val, "r") as inf:
#                 return inf.read()
#         else:
#             return val

#     if output_title:
#         assert output_summary
#         return checks.Output(
#             title = output_title,
#             summary = read_if_file(output_summary),
#             text = read_if_file(output) if output else None
#         )
#     else:
#         return None


def get_buildkite_output():
    # f = open("/home/rbot/.ssh/pass.txt")
    # line = f.readline().strip()
    # f.close()
    # r = requests.get('https://api.buildkite.com/v2/organizations/uber-atg/pipelines/oliver-simulation/builds/87/jobs/40871ae4-d820-433e-8dbd-f59b77f5f9f5/log', auth=('oliver.koo@uber.com', line))
    return '{\n  "url": "https://api.buildkite.com/v2/organizations/uber-atg/pipelines/oliver-simulation/builds/87/jobs/40871ae4-d820-433e-8dbd-f59b77f5f9f5/log",\n  "content": "~~~ Setting up plugins\\r\\n\\u001B[90m# Plugin \\"github.com/buildkite-plugins/docker-compose-buildkite-plugin#v2.5.0\\" already checked out (0df24a9)\\u001B[0m\\r\\n\\u001B[90m# Plugin \\"github.com/OliverKoo/github-checks-buildkite-plugin#v0.0.4\\" already checked out (50e8fa2)\\u001B[0m\\r\\n~~~ Preparing working directory\\r\\n\\u001B[90m$\\u001B[0m cd /home/rbot/.buildkite-agent/builds/rna-jenkins-builder-ip-10-105-9-61-1/uber-atg/oliver-simulation\\r\\n\\u001B[90m# Host \\"github.com\\" already in list of known hosts at \\"/home/rbot/.ssh/known_hosts\\"\\u001B[0m\\r\\n\\u001B[90m$\\u001B[0m git remote set-url origin git@github.com:OliverKoo/simulation.git\\r\\n\\u001B[90m$\\u001B[0m git clean -ffxdq\\r\\n\\u001B[90m# Fetch and checkout pull request head from GitHub\\u001B[0m\\r\\n\\u001B[90m$\\u001B[0m git fetch -v origin refs/pull/14/head\\r\\nWarning: Permanently added \'github.com,192.30.253.113\' (RSA) to the list of known hosts.\\r\\r\\r\\nFrom github.com:OliverKoo/simulation\\r\\r\\n * branch            refs/pull/14/head -> FETCH_HEAD\\r\\r\\n\\u001B[90m# FETCH_HEAD is now `849e85ca66f877081b68d9f41aef934499682154`\\u001B[0m\\r\\n\\u001B[90m$\\u001B[0m git checkout -f 849e85ca66f877081b68d9f41aef934499682154\\r\\nHEAD is now at 849e85c... add docker-compose.yml\\r\\r\\n\\u001B[90m# Cleaning again to catch any post-checkout changes\\u001B[0m\\r\\n\\u001B[90m$\\u001B[0m git clean -ffxdq\\r\\n\\u001B[90m# Checking to see if Git data needs to be sent to Buildkite\\u001B[0m\\r\\n\\u001B[90m$\\u001B[0m buildkite-agent meta-data exists buildkite:git:commit\\r\\n~~~ Setting up vendored plugins\\r\\n~~~ Running plugin github-checks pre-command hook\\r\\n\\u001B[90m$\\u001B[0m /home/rbot/.buildkite-agent/plugins/github-com-OliverKoo-github-checks-buildkite-plugin-v0-0-4/hooks/pre-command\\r\\nAV_USE_DSS=true\\r\\r\\nAWS_BUILD_MACHINE=true\\r\\r\\nBASH=/bin/bash\\r\\r\\nBASHOPTS=cmdhist:complete_fullquote:extquote:force_fignore:hostcomplete:interactive_comments:progcomp:promptvars:sourcepath\\r\\r\\nBASH_ALIASES=()\\r\\r\\nBASH_ARGC=([0]=\\"1\\")\\r\\r\\nBASH_ARGV=([0]=\\"/home/rbot/.buildkite-agent/plugins/github-com-OliverKoo-github-checks-buildkite-plugin-v0-0-4/hooks/pre-command\\")\\r\\r\\nBASH_CMDS=()\\r\\r\\nBASH_LINENO=([0]=\\"2\\" [1]=\\"0\\")\\r\\r\\nBASH_REMATCH=([0]=\\"true\\" [1]=\\"true\\")\\r\\r\\nBASH_SOURCE=([0]=\\"/home/rbot/.buildkite-agent/plugins/github-com-OliverKoo-github-checks-buildkite-plugin-v0-0-4/hooks/pre-command\\" [1]=\\"/tmp/buildkite-agent-bootstrap-hook-runner-935308367\\")\\r\\r\\nBASH_VERSINFO=([0]=\\"4\\" [1]=\\"3\\" [2]=\\"11\\" [3]=\\"1\\" [4]=\\"release\\" [5]=\\"x86_64-pc-linux-gnu\\")\\r\\r\\nBASH_VERSION=\'4.3.11(1)-release\'\\r\\r\\nBUILDKITE=true\\r\\r\\nBUILDKITE_AGENT_ACCESS_TOKEN=4SGM4cJGDkJjLVoxtjvu42742DJd9GSiK11U5mvH8VaiqMLwGC\\r\\r\\nBUILDKITE_AGENT_DEBUG=false\\r\\r\\nBUILDKITE_AGENT_ENDPOINT=https://agent.buildkite.com/v3\\r\\r\\nBUILDKITE_AGENT_EXPERIMENT=\\r\\r\\nBUILDKITE_AGENT_ID=717a5900-3215-4298-a601-8e92a2dcf1f7\\r\\r\\nBUILDKITE_AGENT_NAME=rna-jenkins-builder-ip-10-105-9-61-1\\r\\r\\nBUILDKITE_AGENT_PID=120729\\r\\r\\nBUILDKITE_ARTIFACT_PATHS=\\r\\r\\nBUILDKITE_BIN_PATH=/home/rbot/.buildkite-agent/bin\\r\\r\\nBUILDKITE_BRANCH=add_build_ymal\\r\\r\\nBUILDKITE_BUILD_CHECKOUT_PATH=/home/rbot/.buildkite-agent/builds/rna-jenkins-builder-ip-10-105-9-61-1/uber-atg/oliver-simulation\\r\\r\\nBUILDKITE_BUILD_CREATOR=\'Oliver Koo\'\\r\\r\\nBUILDKITE_BUILD_CREATOR_EMAIL=oliver.koo@uber.com\\r\\r\\nBUILDKITE_BUILD_CREATOR_TEAMS=ci-eval:everyone\\r\\r\\nBUILDKITE_BUILD_ID=46ed1fa8-904f-4e59-837a-3bd72fdf4d03\\r\\r\\nBUILDKITE_BUILD_NUMBER=87\\r\\r\\nBUILDKITE_BUILD_PATH=/home/rbot/.buildkite-agent/builds\\r\\r\\nBUILDKITE_BUILD_URL=https://buildkite.com/uber-atg/oliver-simulation/builds/87\\r\\r\\nBUILDKITE_COMMAND=.buildkite/bin/tests\\r\\r\\nBUILDKITE_COMMAND_EVAL=true\\r\\r\\nBUILDKITE_COMMIT=849e85ca66f877081b68d9f41aef934499682154\\r\\r\\nBUILDKITE_CONFIG_PATH=\'$HOME/.buildkite-agent/buildkite-agent.cfg\'\\r\\r\\nBUILDKITE_ENV_FILE=/tmp/job-env-40871ae4-d820-433e-8dbd-f59b77f5f9f5437089339\\r\\r\\nBUILDKITE_GIT_CLEAN_FLAGS=-ffxdq\\r\\r\\nBUILDKITE_GIT_CLONE_FLAGS=-v\\r\\r\\nBUILDKITE_GIT_SUBMODULES=true\\r\\r\\nBUILDKITE_HOOKS_PATH=/home/rbot/.buildkite-agent/hooks\\r\\r\\nBUILDKITE_JOB_ID=40871ae4-d820-433e-8dbd-f59b77f5f9f5\\r\\r\\nBUILDKITE_LABEL=\'Plugin Tests\'\\r\\r\\nBUILDKITE_LOCAL_HOOKS_ENABLED=true\\r\\r\\nBUILDKITE_MESSAGE=\'add docker-compose.yml\'\\r\\r\\nBUILDKITE_ORGANIZATION_SLUG=uber-atg\\r\\r\\nBUILDKITE_PIPELINE_DEFAULT_BRANCH=master\\r\\r\\nBUILDKITE_PIPELINE_PROVIDER=github\\r\\r\\nBUILDKITE_PIPELINE_SLUG=oliver-simulation\\r\\r\\nBUILDKITE_PLUGINS=\'[{\\"github.com/buildkite-plugins/docker-compose-buildkite-plugin#v2.5.0\\":{\\"run\\":\\"tests\\",\\"workdir\\":\\"/home/rbot/.buildkite-agent/builds/rna-jenkins-builder-ip-10-105-9-61-1/uber-atg/oliver-simulation\\"}},{\\"github.com/OliverKoo/github-checks-buildkite-plugin#v0.0.4\\":{\\"debug\\":true,\\"output_title\\":\\"buildkite/plugin-tests\\",\\"output_summary\\":\\"test_summary.md\\"}}]\'\\r\\r\\nBUILDKITE_PLUGINS_ENABLED=true\\r\\r\\nBUILDKITE_PLUGINS_PATH=/home/rbot/.buildkite-agent/plugins\\r\\r\\nBUILDKITE_PLUGIN_GITHUB_CHECKS_DEBUG=true\\r\\r\\nBUILDKITE_PLUGIN_GITHUB_CHECKS_OUTPUT_SUMMARY=test_summary.md\\r\\r\\nBUILDKITE_PLUGIN_GITHUB_CHECKS_OUTPUT_TITLE=buildkite/plugin-tests\\r\\r\\nBUILDKITE_PLUGIN_VALIDATION=false\\r\\r\\nBUILDKITE_PROJECT_PROVIDER=github\\r\\r\\nBUILDKITE_PROJECT_SLUG=uber-atg/oliver-simulation\\r\\r\\nBUILDKITE_PULL_REQUEST=14\\r\\r\\nBUILDKITE_PULL_REQUEST_BASE_BRANCH=master\\r\\r\\nBUILDKITE_PULL_REQUEST_REPO=git://github.com/OliverKoo/simulation.git\\r\\r\\nBUILDKITE_REBUILT_FROM_BUILD_ID=\\r\\r\\nBUILDKITE_REBUILT_FROM_BUILD_NUMBER=\\r\\r\\nBUILDKITE_REPO=git@github.com:OliverKoo/simulation.git\\r\\r\\nBUILDKITE_REPO_SSH_HOST=github.com\\r\\r\\nBUILDKITE_RETRY_COUNT=0\\r\\r\\nBUILDKITE_SCRIPT_PATH=.buildkite/bin/tests\\r\\r\\nBUILDKITE_SHELL=\'/bin/bash -e -c\'\\r\\r\\nBUILDKITE_SOURCE=webhook\\r\\r\\nBUILDKITE_SSH_KEYSCAN=true\\r\\r\\nBUILDKITE_TAG=\\r\\r\\nBUILDKITE_TIMEOUT=false\\r\\r\\nBUILDKITE_TRIGGERED_FROM_BUILD_ID=\\r\\r\\nBUILDKITE_TRIGGERED_FROM_BUILD_NUMBER=\\r\\r\\nBUILDKITE_TRIGGERED_FROM_BUILD_PIPELINE_SLUG=\\r\\r\\nCI=true\\r\\r\\nCOMPOSE_API_VERSION=auto\\r\\r\\nDIRSTACK=()\\r\\r\\nEUID=15211\\r\\r\\nGID=15000\\r\\r\\nGITHUB_APP_AUTH_ID=26924\\r\\r\\nGITHUB_APP_AUTH_KEY=/home/rbot/.ssh/private-key.pem\\r\\r\\nGIT_TERMINAL_PROMPT=0\\r\\r\\nGROUPS=()\\r\\r\\nHOME=/home/rbot\\r\\r\\nHOSTNAME=rna-jenkins-builder-ip-10-105-9-61\\r\\r\\nHOSTTYPE=x86_64\\r\\r\\nIFS=$\' \\\\t\\\\n\'\\r\\r\\nLANG=en_US.UTF-8\\r\\r\\nLOGNAME=rbot\\r\\r\\nMACHTYPE=x86_64-pc-linux-gnu\\r\\r\\nMAIL=/var/mail/rbot\\r\\r\\nOPTERR=1\\r\\r\\nOPTIND=1\\r\\r\\nOSTYPE=linux-gnu\\r\\r\\nPATH=/home/rbot/.buildkite-agent/bin:/usr/local/bin:/usr/bin:/bin:/usr/local/games:/usr/games\\r\\r\\nPIPESTATUS=([0]=\\"0\\")\\r\\r\\nPPID=578\\r\\r\\nPS4=\'+ \'\\r\\r\\nPWD=/home/rbot/.buildkite-agent/builds/rna-jenkins-builder-ip-10-105-9-61-1/uber-atg/oliver-simulation\\r\\r\\nSHELL=/bin/bash\\r\\r\\nSHELLOPTS=braceexpand:errexit:hashall:interactive-comments:nounset:pipefail\\r\\r\\nSHLVL=3\\r\\r\\nSSH_AGENT_PID=41648\\r\\r\\nSSH_AUTH_SOCK=/tmp/ssh-0lZoJrX4kHlV/agent.41647\\r\\r\\nTERM=xterm-256color\\r\\r\\nUID=15211\\r\\r\\nUSER=rbot\\r\\r\\nVIRTUALENVWRAPPER_SCRIPT=/usr/share/virtualenvwrapper/virtualenvwrapper.sh\\r\\r\\nXDG_RUNTIME_DIR=/run/user/1000\\r\\r\\nXDG_SESSION_ID=3\\r\\r\\n_=pipefail\\r\\r\\n_VIRTUALENVWRAPPER_API=\' mkvirtualenv rmvirtualenv lsvirtualenv showvirtualenv workon add2virtualenv cdsitepackages cdvirtualenv lssitepackages toggleglobalsitepackages cpvirtualenv setvirtualenvproject mkproject cdproject mktmpenv\'\\r\\r\\n+ export LC_ALL=C.UTF-8\\r\\r\\n+ LC_ALL=C.UTF-8\\r\\r\\n+ export LANG=C.UTF-8\\r\\r\\n+ LANG=C.UTF-8\\r\\r\\n++ dirname /home/rbot/.buildkite-agent/plugins/github-com-OliverKoo-github-checks-buildkite-plugin-v0-0-4/hooks/ghapp\\r\\r\\n+ COMPOSE_CONFIG=/home/rbot/.buildkite-agent/plugins/github-com-OliverKoo-github-checks-buildkite-plugin-v0-0-4/hooks/../docker-compose.yml\\r\\r\\n+ run_params=()\\r\\r\\n+ args=()\\r\\r\\n+ docker-compose -f /home/rbot/.buildkite-agent/plugins/github-com-OliverKoo-github-checks-buildkite-plugin-v0-0-4/hooks/../docker-compose.yml build ghapp\\r\\r\\n/usr/local/lib/python2.7/dist-packages/requests/__init__.py:80: RequestsDependencyWarning: urllib3 (1.23) or chardet (3.0.4) doesn\'t match a supported version!\\r\\r\\n  RequestsDependencyWarning)\\r\\r\\n/usr/local/lib/python2.7/dist-packages/cryptography/hazmat/primitives/constant_time.py:26: CryptographyDeprecationWarning: Support for your Python version is deprecated. The next version of cryptography will remove support. Please upgrade to a 2.7.x release that supports hmac.compare_digest as soon as possible.\\r\\r\\n  utils.DeprecatedIn23,\\r\\r\\nBuilding ghapp\\r\\r\\nStep 1/4 : FROM alpine:latest\\r\\r\\n ---> 5cb3aa00f899\\r\\r\\nStep 2/4 : RUN apk add --no-cache python3 py3-cryptography bash\\r\\r\\n ---> Using cache\\r\\r\\n ---> 3763b6e0fbd9\\r\\r\\nStep 3/4 : COPY . /ghapp\\r\\r\\n ---> Using cache\\r\\r\\n ---> 2f967bac3fd1\\r\\r\\nStep 4/4 : RUN pip3 install /ghapp\\r\\r\\n ---> Using cache\\r\\r\\n ---> c804897a1de7\\r\\r\\n\\u001B[2K\\r\\r\\r\\r\\nSuccessfully built c804897a1de7\\r\\r\\nSuccessfully tagged github-com-oliverkoo-github-checks-buildkite-plugin-v0-0-4_ghapp:latest\\r\\r\\n+ docker volume create --name=buildkite\\r\\r\\nbuildkite\\r\\r\\n+ [[ true =~ (true|on|1) ]]\\r\\r\\n+ args+=(\\"-vv\\")\\r\\r\\n+ [[ -n \'\' ]]\\r\\r\\n+ [[ -f 26924 ]]\\r\\r\\n+ [[ -n \'\' ]]\\r\\r\\n+ set +x\\r\\r\\n+ GITHUB_APP_AUTH_KEY=$(cat /home/rbot/.ssh/private-key.pem)\\r\\r\\n+ IFS=\';\'\\r\\r\\n+ read -r -a default_volumes\\r\\r\\n+ for vol in \'\\"${default_volumes[@]:-}\\"\'\\r\\r\\n+ [[ ! -z \'\' ]]\\r\\r\\n++ pwd\\r\\r\\n+ docker-compose -f /home/rbot/.buildkite-agent/plugins/github-com-OliverKoo-github-checks-buildkite-plugin-v0-0-4/hooks/../docker-compose.yml run --workdir=/home/rbot/.buildkite-agent/builds/rna-jenkins-builder-ip-10-105-9-61-1/uber-atg/oliver-simulation --rm ghapp -vv check from-job-env\\r\\r\\n/usr/local/lib/python2.7/dist-packages/requests/__init__.py:80: RequestsDependencyWarning: urllib3 (1.23) or chardet (3.0.4) doesn\'t match a supported version!\\r\\r\\n  RequestsDependencyWarning)\\r\\r\\n/usr/local/lib/python2.7/dist-packages/cryptography/hazmat/primitives/constant_time.py:26: CryptographyDeprecationWarning: Support for your Python version is deprecated. The next version of cryptography will remove support. Please upgrade to a 2.7.x release that supports hmac.compare_digest as soon as possible.\\r\\r\\n  utils.DeprecatedIn23,\\r\\r\\nCreating network \\"github-com-oliverkoo-github-checks-buildkite-plugin-v0-0-4_default\\" with the default driver\\r\\r\\nghapp.github.identity Resolved app_id: 26924\\r\\r\\nghapp.github.identity Resolved private key from value.\\r\\r\\nghapp.github.identity Resolved private key.\\r\\r\\nasyncio Using selector: EpollSelector\\r\\r\\nroot job_env: JobEnviron(CI=1, BUILDKITE=1, BUILDKITE_LABEL=\'Plugin Tests\', BUILDKITE_BRANCH=\'add_build_ymal\', BUILDKITE_COMMIT=\'849e85ca66f877081b68d9f41aef934499682154\', BUILDKITE_REPO=\'git@github.com:OliverKoo/simulation.git\', BUILDKITE_BUILD_ID=\'46ed1fa8-904f-4e59-837a-3bd72fdf4d03\', BUILDKITE_BUILD_NUMBER=\'87\', BUILDKITE_BUILD_URL=\'https://buildkite.com/uber-atg/oliver-simulation/builds/87\', BUILDKITE_JOB_ID=\'40871ae4-d820-433e-8dbd-f59b77f5f9f5\', BUILDKITE_COMMAND=\'.buildkite/bin/tests\', BUILDKITE_TIMEOUT=0, BUILDKITE_COMMAND_EXIT_STATUS=None)\\r\\r\\nroot Issuing app jwt: {\'iat\': 1552592451, \'exp\': 1552593051, \'iss\': 26924}\\r\\r\\nghapp.github.checks <ClientResponse(https://api.github.com/repos/OliverKoo/simulation/commits/849e85ca66f877081b68d9f41aef934499682154/check-runs) [200 OK]>\\r\\r\\n<CIMultiDictProxy(\'Server\': \'GitHub.com\', \'Date\': \'Thu, 14 Mar 2019 19:40:51 GMT\', \'Content-Type\': \'application/json; charset=utf-8\', \'Transfer-Encoding\': \'chunked\', \'Status\': \'200 OK\', \'X-RateLimit-Limit\': \'5000\', \'X-RateLimit-Remaining\': \'4935\', \'X-RateLimit-Reset\': \'1552594903\', \'Cache-Control\': \'private, max-age=60, s-maxage=60\', \'Vary\': \'Accept, Authorization, Cookie, X-GitHub-OTP\', \'ETag\': \'W/\\"8793b6e48893f15ca7e07ca403471706\\"\', \'X-GitHub-Media-Type\': \'github.antiope-preview; format=json\', \'Access-Control-Expose-Headers\': \'ETag, Link, Location, Retry-After, X-GitHub-OTP, X-RateLimit-Limit, X-RateLimit-Remaining, X-RateLimit-Reset, X-OAuth-Scopes,X-Accepted-OAuth-Scopes, X-Poll-Interval, X-GitHub-Media-Type\', \'Access-Control-Allow-Origin\': \'*\', \'Strict-Transport-Security\': \'max-age=31536000; includeSubdomains; preload\', \'X-Frame-Options\': \'deny\', \'X-Content-Type-Options\': \'nosniff\', \'X-XSS-Protection\': \'1; mode=block\', \'Referrer-Policy\': \'origin-when-cross-origin, strict-origin-when-cross-origin\', \'Content-Security-Policy\': \\"default-src \'none\'\\", \'Content-Encoding\': \'gzip\', \'X-GitHub-Request-Id\': \'B4F0:1DA1:803502:FBC653:5C8AAE43\')>\\r\\r\\n\\r\\r\\nroot current_runs: []\\r\\r\\nroot action: CreateRun(owner=\'OliverKoo\', repo=\'simulation\', run=RunDetails(name=\'Plugin Tests\', id=None, head_sha=\'849e85ca66f877081b68d9f41aef934499682154\', head_branch=\'add_build_ymal\', details_url=\'https://buildkite.com/uber-atg/oliver-simulation/builds/87#40871ae4-d820-433e-8dbd-f59b77f5f9f5\', external_id=\'40871ae4-d820-433e-8dbd-f59b77f5f9f5\', status=<Status.in_progress: \'in_progress\'>, started_at=\'2019-03-14T19:40:51Z\', conclusion=None, completed_at=None, output=None))\\r\\r\\nghapp.github.checks POST https://api.github.com/repos/OliverKoo/simulation/check-runs\\r\\r\\n{\'name\': \'Plugin Tests\', \'head_sha\': \'849e85ca66f877081b68d9f41aef934499682154\', \'head_branch\': \'add_build_ymal\', \'details_url\': \'https://buildkite.com/uber-atg/oliver-simulation/builds/87#40871ae4-d820-433e-8dbd-f59b77f5f9f5\', \'external_id\': \'40871ae4-d820-433e-8dbd-f59b77f5f9f5\', \'status\': \'in_progress\',\'started_at\': \'2019-03-14T19:40:51Z\'}\\r\\r\\n+ docker-compose -f /home/rbot/.buildkite-agent/plugins/github-com-OliverKoo-github-checks-buildkite-plugin-v0-0-4/hooks/../docker-compose.yml down\\r\\r\\n/usr/local/lib/python2.7/dist-packages/requests/__init__.py:80: RequestsDependencyWarning: urllib3 (1.23) or chardet (3.0.4) doesn\'t match a supported version!\\r\\r\\n  RequestsDependencyWarning)\\r\\r\\n/usr/local/lib/python2.7/dist-packages/cryptography/hazmat/primitives/constant_time.py:26: CryptographyDeprecationWarning: Support for your Python version is deprecated. The next version of cryptography will remove support. Please upgrade to a 2.7.x release that supports hmac.compare_digest as soon as possible.\\r\\r\\n  utils.DeprecatedIn23,\\r\\r\\nRemoving network github-com-oliverkoo-github-checks-buildkite-plugin-v0-0-4_default\\r\\r\\n~~~ Running plugin docker-compose command hook\\r\\n\\u001B[90m$\\u001B[0m /home/rbot/.buildkite-agent/plugins/github-com-buildkite-plugins-docker-compose-buildkite-plugin-v2-5-0/hooks/command\\r\\n\\u001B[90m$\\u001B[0m buildkite-agent meta-data get docker-compose-plugin-built-image-tag-tests\\r\\r\\n\\u001B[31m2019-03-14 19:40:52 FATAL \\u001B[0m \\u001B[31mFailed to get meta-data: POST https://agent.buildkite.com/v3/jobs/40871ae4-d820-433e-8dbd-f59b77f5f9f5/data/get: 404 No key \\"docker-compose-plugin-built-image-tag-te...\\" found\\u001B[0m\\r\\r\\n~~~ :docker: Building Docker Compose Service: tests\\r\\r\\n⚠️ No pre-built image found from a previous \'build\' step for this service and config file. Building image...\\r\\r\\n\\u001B[90m$\\u001B[0m docker-compose -f docker-compose.yml -p buildkite40871ae4d820433e8dbdf59b77f5f9f5 build --pull tests\\r\\r\\n/usr/local/lib/python2.7/dist-packages/requests/__init__.py:80: RequestsDependencyWarning: urllib3 (1.23) or chardet (3.0.4) doesn\'t match a supported version!\\r\\r\\n  RequestsDependencyWarning)\\r\\r\\n/usr/local/lib/python2.7/dist-packages/cryptography/hazmat/primitives/constant_time.py:26: CryptographyDeprecationWarning: Support for your Python version is deprecated. The next version of cryptography will remove support. Please upgrade to a 2.7.x release that supports hmac.compare_digest as soon as possible.\\r\\r\\n  utils.DeprecatedIn23,\\r\\r\\n\\u001B[31mERROR\\u001B[0m: build path /home/rbot/.buildkite-agent/builds/rna-jenkins-builder-ip-10-105-9-61-1/uber-atg/oliver-simulation/ghapp either does not exist, is not accessible, or is not a valid URL.\\r\\r\\n^^^ +++\\r\\r\\n~~~ :docker: Cleaning up after docker-compose\\r\\r\\n\\u001B[90m$\\u001B[0m docker-compose -f docker-compose.yml -p buildkite40871ae4d820433e8dbdf59b77f5f9f5 kill\\r\\r\\n/usr/local/lib/python2.7/dist-packages/requests/__init__.py:80: RequestsDependencyWarning: urllib3 (1.23) or chardet (3.0.4) doesn\'t match a supported version!\\r\\r\\n  RequestsDependencyWarning)\\r\\r\\n/usr/local/lib/python2.7/dist-packages/cryptography/hazmat/primitives/constant_time.py:26: CryptographyDeprecationWarning: Support foryour Python version is deprecated. The next version of cryptography will remove support. Please upgrade to a 2.7.x release that supports hmac.compare_digest as soon as possible.\\r\\r\\n  utils.DeprecatedIn23,\\r\\r\\n\\u001B[31mERROR\\u001B[0m: build path /home/rbot/.buildkite-agent/builds/rna-jenkins-builder-ip-10-105-9-61-1/uber-atg/oliver-simulation/ghapp either does not exist, is not accessible, or is not a valid URL.\\r\\r\\n\\u001B[90m$\\u001B[0m docker-compose -f docker-compose.yml -p buildkite40871ae4d820433e8dbdf59b77f5f9f5 rm --force -v\\r\\r\\n/usr/local/lib/python2.7/dist-packages/requests/__init__.py:80: RequestsDependencyWarning: urllib3 (1.23) or chardet (3.0.4) doesn\'t match a supported version!\\r\\r\\n  RequestsDependencyWarning)\\r\\r\\n/usr/local/lib/python2.7/dist-packages/cryptography/hazmat/primitives/constant_time.py:26: CryptographyDeprecationWarning: Support for your Python version is deprecated. The next version of cryptography will remove support. Please upgrade to a 2.7.x release that supports hmac.compare_digest as soon as possible.\\r\\r\\n  utils.DeprecatedIn23,\\r\\r\\n\\u001B[31mERROR\\u001B[0m: build path /home/rbot/.buildkite-agent/builds/rna-jenkins-builder-ip-10-105-9-61-1/uber-atg/oliver-simulation/ghapp either does not exist, is not accessible, or is not a valid URL.\\r\\r\\n\\u001B[90m$\\u001B[0m docker-compose -f docker-compose.yml -p buildkite40871ae4d820433e8dbdf59b77f5f9f5 down --volumes\\r\\r\\n/usr/local/lib/python2.7/dist-packages/requests/__init__.py:80: RequestsDependencyWarning: urllib3 (1.23) or chardet (3.0.4) doesn\'t match a supported version!\\r\\r\\n  RequestsDependencyWarning)\\r\\r\\n/usr/local/lib/python2.7/dist-packages/cryptography/hazmat/primitives/constant_time.py:26: CryptographyDeprecationWarning: Support for your Python version is deprecated. The next version of cryptography will remove support. Please upgrade to a 2.7.x release that supports hmac.compare_digest as soon as possible.\\r\\r\\n  utils.DeprecatedIn23,\\r\\r\\n\\u001B[31mERROR\\u001B[0m: build path /home/rbot/.buildkite-agent/builds/rna-jenkins-builder-ip-10-105-9-61-1/uber-atg/oliver-simulation/ghapp either does not exist, is not accessible, or is not a valid URL.\\r\\r\\n\\u001B[31m🚨 Error: The commandexited with status 1\\u001B[0m\\r\\n^^^ +++\\r\\n^^^ +++\\r\\n~~~ Running plugin github-checks post-command hook\\r\\n\\u001B[90m$\\u001B[0m /home/rbot/.buildkite-agent/plugins/github-com-OliverKoo-github-checks-buildkite-plugin-v0-0-4/hooks/post-command\\r\\nAV_USE_DSS=true\\r\\r\\nAWS_BUILD_MACHINE=true\\r\\r\\nBASH=/bin/bash\\r\\r\\nBASHOPTS=cmdhist:complete_fullquote:extquote:force_fignore:hostcomplete:interactive_comments:progcomp:promptvars:sourcepath\\r\\r\\nBASH_ALIASES=()\\r\\r\\nBASH_ARGC=([0]=\\"1\\")\\r\\r\\nBASH_ARGV=([0]=\\"/home/rbot/.buildkite-agent/plugins/github-com-OliverKoo-github-checks-buildkite-plugin-v0-0-4/hooks/post-command\\")\\r\\r\\nBASH_CMDS=()\\r\\r\\nBASH_LINENO=([0]=\\"2\\" [1]=\\"0\\")\\r\\r\\nBASH_REMATCH=([0]=\\"true\\" [1]=\\"true\\")\\r\\r\\nBASH_SOURCE=([0]=\\"/home/rbot/.buildkite-agent/plugins/github-com-OliverKoo-github-checks-buildkite-plugin-v0-0-4/hooks/post-command\\" [1]=\\"/tmp/buildkite-agent-bootstrap-hook-runner-397282717\\")\\r\\r\\nBASH_VERSINFO=([0]=\\"4\\" [1]=\\"3\\" [2]=\\"11\\" [3]=\\"1\\" [4]=\\"release\\" [5]=\\"x86_64-pc-linux-gnu\\")\\r\\r\\nBASH_VERSION=\'4.3.11(1)-release\'\\r\\r\\nBUILDKITE=true\\r\\r\\nBUILDKITE_AGENT_ACCESS_TOKEN=4SGM4cJGDkJjLVoxtjvu42742DJd9GSiK11U5mvH8VaiqMLwGC\\r\\r\\nBUILDKITE_AGENT_DEBUG=false\\r\\r\\nBUILDKITE_AGENT_ENDPOINT=https://agent.buildkite.com/v3\\r\\r\\nBUILDKITE_AGENT_EXPERIMENT=\\r\\r\\nBUILDKITE_AGENT_ID=717a5900-3215-4298-a601-8e92a2dcf1f7\\r\\r\\nBUILDKITE_AGENT_NAME=rna-jenkins-builder-ip-10-105-9-61-1\\r\\r\\nBUILDKITE_AGENT_PID=120729\\r\\r\\nBUILDKITE_ARTIFACT_PATHS=\\r\\r\\nBUILDKITE_BIN_PATH=/home/rbot/.buildkite-agent/bin\\r\\r\\nBUILDKITE_BRANCH=add_build_ymal\\r\\r\\nBUILDKITE_BUILD_CHECKOUT_PATH=/home/rbot/.buildkite-agent/builds/rna-jenkins-builder-ip-10-105-9-61-1/uber-atg/oliver-simulation\\r\\r\\nBUILDKITE_BUILD_CREATOR=\'Oliver Koo\'\\r\\r\\nBUILDKITE_BUILD_CREATOR_EMAIL=oliver.koo@uber.com\\r\\r\\nBUILDKITE_BUILD_CREATOR_TEAMS=ci-eval:everyone\\r\\r\\nBUILDKITE_BUILD_ID=46ed1fa8-904f-4e59-837a-3bd72fdf4d03\\r\\r\\nBUILDKITE_BUILD_NUMBER=87\\r\\r\\nBUILDKITE_BUILD_PATH=/home/rbot/.buildkite-agent/builds\\r\\r\\nBUILDKITE_BUILD_URL=https://buildkite.com/uber-atg/oliver-simulation/builds/87\\r\\r\\nBUILDKITE_COMMAND=.buildkite/bin/tests\\r\\r\\nBUILDKITE_COMMAND_EVAL=true\\r\\r\\nBUILDKITE_COMMAND_EXIT_STATUS=1\\r\\r\\nBUILDKITE_COMMIT=849e85ca66f877081b68d9f41aef934499682154\\r\\r\\nBUILDKITE_CONFIG_PATH=\'$HOME/.buildkite-agent/buildkite-agent.cfg\'\\r\\r\\nBUILDKITE_ENV_FILE=/tmp/job-env-40871ae4-d820-433e-8dbd-f59b77f5f9f5437089339\\r\\r\\nBUILDKITE_GIT_CLEAN_FLAGS=-ffxdq\\r\\r\\nBUILDKITE_GIT_CLONE_FLAGS=-v\\r\\r\\nBUILDKITE_GIT_SUBMODULES=true\\r\\r\\nBUILDKITE_HOOKS_PATH=/home/rbot/.buildkite-agent/hooks\\r\\r\\nBUILDKITE_JOB_ID=40871ae4-d820-433e-8dbd-f59b77f5f9f5\\r\\r\\nBUILDKITE_LABEL=\'Plugin Tests\'\\r\\r\\nBUILDKITE_LAST_HOOK_EXIT_STATUS=1\\r\\r\\nBUILDKITE_LOCAL_HOOKS_ENABLED=true\\r\\r\\nBUILDKITE_MESSAGE=\'add docker-compose.yml\'\\r\\r\\nBUILDKITE_ORGANIZATION_SLUG=uber-atg\\r\\r\\nBUILDKITE_PIPELINE_DEFAULT_BRANCH=master\\r\\r\\nBUILDKITE_PIPELINE_PROVIDER=github\\r\\r\\nBUILDKITE_PIPELINE_SLUG=oliver-simulation\\r\\r\\nBUILDKITE_PLUGINS=\'[{\\"github.com/buildkite-plugins/docker-compose-buildkite-plugin#v2.5.0\\":{\\"run\\":\\"tests\\",\\"workdir\\":\\"/home/rbot/.buildkite-agent/builds/rna-jenkins-builder-ip-10-105-9-61-1/uber-atg/oliver-simulation\\"}},{\\"github.com/OliverKoo/github-checks-buildkite-plugin#v0.0.4\\":{\\"debug\\":true,\\"output_title\\":\\"buildkite/plugin-tests\\",\\"output_summary\\":\\"test_summary.md\\"}}]\'\\r\\r\\nBUILDKITE_PLUGINS_ENABLED=true\\r\\r\\nBUILDKITE_PLUGINS_PATH=/home/rbot/.buildkite-agent/plugins\\r\\r\\nBUILDKITE_PLUGIN_GITHUB_CHECKS_DEBUG=true\\r\\r\\nBUILDKITE_PLUGIN_GITHUB_CHECKS_OUTPUT_SUMMARY=test_summary.md\\r\\r\\nBUILDKITE_PLUGIN_GITHUB_CHECKS_OUTPUT_TITLE=buildkite/plugin-tests\\r\\r\\nBUILDKITE_PLUGIN_VALIDATION=false\\r\\r\\nBUILDKITE_PROJECT_PROVIDER=github\\r\\r\\nBUILDKITE_PROJECT_SLUG=uber-atg/oliver-simulation\\r\\r\\nBUILDKITE_PULL_REQUEST=14\\r\\r\\nBUILDKITE_PULL_REQUEST_BASE_BRANCH=master\\r\\r\\nBUILDKITE_PULL_REQUEST_REPO=git://github.com/OliverKoo/simulation.git\\r\\r\\nBUILDKITE_REBUILT_FROM_BUILD_ID=\\r\\r\\nBUILDKITE_REBUILT_FROM_BUILD_NUMBER=\\r\\r\\nBUILDKITE_REPO=git@github.com:OliverKoo/simulation.git\\r\\r\\nBUILDKITE_REPO_SSH_HOST=github.com\\r\\r\\nBUILDKITE_RETRY_COUNT=0\\r\\r\\nBUILDKITE_SCRIPT_PATH=.buildkite/bin/tests\\r\\r\\nBUILDKITE_SHELL=\'/bin/bash -e -c\'\\r\\r\\nBUILDKITE_SOURCE=webhook\\r\\r\\nBUILDKITE_SSH_KEYSCAN=true\\r\\r\\nBUILDKITE_TAG=\\r\\r\\nBUILDKITE_TIMEOUT=false\\r\\r\\nBUILDKITE_TRIGGERED_FROM_BUILD_ID=\\r\\r\\nBUILDKITE_TRIGGERED_FROM_BUILD_NUMBER=\\r\\r\\nBUILDKITE_TRIGGERED_FROM_BUILD_PIPELINE_SLUG=\\r\\r\\nCI=true\\r\\r\\nCOMPOSE_API_VERSION=auto\\r\\r\\nDIRSTACK=()\\r\\r\\nEUID=15211\\r\\r\\nGID=15000\\r\\r\\nGITHUB_APP_AUTH_ID=26924\\r\\r\\nGITHUB_APP_AUTH_KEY=/home/rbot/.ssh/private-key.pem\\r\\r\\nGIT_TERMINAL_PROMPT=0\\r\\r\\nGROUPS=()\\r\\r\\nHOME=/home/rbot\\r\\r\\nHOSTNAME=rna-jenkins-builder-ip-10-105-9-61\\r\\r\\nHOSTTYPE=x86_64\\r\\r\\nIFS=$\' \\\\t\\\\n\'\\r\\r\\nLANG=en_US.UTF-8\\r\\r\\nLOGNAME=rbot\\r\\r\\nMACHTYPE=x86_64-pc-linux-gnu\\r\\r\\nMAIL=/var/mail/rbot\\r\\r\\nOPTERR=1\\r\\r\\nOPTIND=1\\r\\r\\nOSTYPE=linux-gnu\\r\\r\\nPATH=/home/rbot/.buildkite-agent/bin:/usr/local/bin:/usr/bin:/bin:/usr/local/games:/usr/games\\r\\r\\nPIPESTATUS=([0]=\\"0\\")\\r\\r\\nPPID=578\\r\\r\\nPS4=\'+ \'\\r\\r\\nPWD=/home/rbot/.buildkite-agent/builds/rna-jenkins-builder-ip-10-105-9-61-1/uber-atg/oliver-simulation\\r\\r\\nSHELL=/bin/bash\\r\\r\\nSHELLOPTS=braceexpand:errexit:hashall:interactive-comments:nounset:pipefail\\r\\r\\nSHLVL=3\\r\\r\\nSSH_AGENT_PID=41648\\r\\r\\nSSH_AUTH_SOCK=/tmp/ssh-0lZoJrX4kHlV/agent.41647\\r\\r\\nTERM=xterm-256color\\r\\r\\nUID=15211\\r\\r\\nUSER=rbot\\r\\r\\nVIRTUALENVWRAPPER_SCRIPT=/usr/share/virtualenvwrapper/virtualenvwrapper.sh\\r\\r\\nXDG_RUNTIME_DIR=/run/user/1000\\r\\r\\nXDG_SESSION_ID=3\\r\\r\\n_=pipefail\\r\\r\\n_VIRTUALENVWRAPPER_API=\' mkvirtualenv rmvirtualenv lsvirtualenv showvirtualenv workon add2virtualenv cdsitepackages cdvirtualenv lssitepackages toggleglobalsitepackages cpvirtualenv setvirtualenvproject mkproject cdproject mktmpenv\'\\r\\r\\n/home/rbot/.buildkite-agent/builds/rna-jenkins-builder-ip-10-105-9-61-1/uber-atg/oliver-simulation\\r\\r\\ntotal 68\\r\\r\\ndrwxr-xr-x  2 rbot uberatc  4096 Mar 13 22:35 artifacts\\r\\r\\ndrwxr-xr-x 32 rbot uberatc  4096 Mar 12 20:54 deployables\\r\\r\\n-rw-r--r--  1 rbot uberatc   818 Mar 14 19:37 docker-compose.yml\\r\\r\\ndrwxr-xr-x  2 rbot uberatc  4096 Mar 12 20:54 docs\\r\\r\\ndrwxr-xr-x  6 rbot uberatc  4096 Mar 12 20:54 lib\\r\\r\\ndrwxr-xr-x  2 rbot uberatc  4096 Mar12 20:54 oliver\\r\\r\\n-rw-r--r--  1 rbot uberatc  6641 Mar 12 20:54 README.md\\r\\r\\n-rwxr-xr-x  1 rbot uberatc 14178 Mar 12 20:54 release\\r\\r\\n-rw-r--r--  1 rbot uberatc    34 Mar 14 19:40 result.txt\\r\\r\\n-rwxr-xr-x  1 rbot uberatc  3558 Mar 12 20:54 run\\r\\r\\ndrwxr-xr-x  7 rbot uberatc  4096 Mar 12 20:54 samples\\r\\r\\ndrwxr-xr-x  3 rbot uberatc  4096 Mar 12 20:54 third_party\\r\\r\\ndrwxr-xr-x  9 rbot uberatc  4096 Mar 12 20:54 tools\\r\\r\\n+ export LC_ALL=C.UTF-8\\r\\r\\n+ LC_ALL=C.UTF-8\\r\\r\\n+ export LANG=C.UTF-8\\r\\r\\n+ LANG=C.UTF-8\\r\\r\\n++ dirname /home/rbot/.buildkite-agent/plugins/github-com-OliverKoo-github-checks-buildkite-plugin-v0-0-4/hooks/ghapp\\r\\r\\n+ COMPOSE_CONFIG=/home/rbot/.buildkite-agent/plugins/github-com-OliverKoo-github-checks-buildkite-plugin-v0-0-4/hooks/../docker-compose.yml\\r\\r\\n+ run_params=()\\r\\r\\n+ args=()\\r\\r\\n+ docker-compose -f /home/rbot/.buildkite-agent/plugins/github-com-OliverKoo-github-checks-buildkite-plugin-v0-0-4/hooks/../docker-compose.yml build ghapp\\r\\r\\n/usr/local/lib/python2.7/dist-packages/requests/__init__.py:80: RequestsDependencyWarning: urllib3 (1.23) or chardet (3.0.4) doesn\'t match a supported version!\\r\\r\\n  RequestsDependencyWarning)\\r\\r\\n/usr/local/lib/python2.7/dist-packages/cryptography/hazmat/primitives/constant_time.py:26: CryptographyDeprecationWarning: Support for your Python version is deprecated. The next version of cryptography will remove support. Please upgrade to a 2.7.x release that supports hmac.compare_digest as soon as possible.\\r\\r\\n  utils.DeprecatedIn23,\\r\\r\\nBuilding ghapp\\r\\r\\nStep 1/4 : FROM alpine:latest\\r\\r\\n ---> 5cb3aa00f899\\r\\r\\nStep 2/4 : RUN apk add --no-cache python3 py3-cryptography bash\\r\\r\\n ---> Using cache\\r\\r\\n ---> 3763b6e0fbd9\\r\\r\\nStep 3/4 : COPY . /ghapp\\r\\r\\n ---> Using cache\\r\\r\\n ---> 2f967bac3fd1\\r\\r\\nStep 4/4 : RUN pip3 install /ghapp\\r\\r\\n ---> Using cache\\r\\r\\n ---> c804897a1de7\\r\\r\\n\\u001B[2K\\r\\r\\r\\r\\nSuccessfully built c804897a1de7\\r\\r\\nSuccessfully tagged github-com-oliverkoo-github-checks-buildkite-plugin-v0-0-4_ghapp:latest\\r\\r\\n+ docker volume create --name=buildkite\\r\\r\\nbuildkite\\r\\r\\n+ [[ true =~ (true|on|1) ]]\\r\\r\\n+ args+=(\\"-vv\\")\\r\\r\\n+ [[ -n \'\' ]]\\r\\r\\n+ [[ -f 26924 ]]\\r\\r\\n+ [[ -n \'\' ]]\\r\\r\\n+ set +x\\r\\r\\n+ GITHUB_APP_AUTH_KEY=$(cat /home/rbot/.ssh/private-key.pem)\\r\\r\\n+ IFS=\';\'\\r\\r\\n+ read -r -a default_volumes\\r\\r\\n+ for vol in \'\\"${default_volumes[@]:-}\\"\'\\r\\r\\n+ [[ ! -z \'\' ]]\\r\\r\\n++ pwd\\r\\r\\n+ docker-compose -f /home/rbot/.buildkite-agent/plugins/github-com-OliverKoo-github-checks-buildkite-plugin-v0-0-4/hooks/../docker-compose.yml run --workdir=/home/rbot/.buildkite-agent/builds/rna-jenkins-builder-ip-10-105-9-61-1/uber-atg/oliver-simulation --rm ghapp -vv check from-job-env --output_title buildkite/plugin-tests --output_summary test_summary.md\\r\\r\\n/usr/local/lib/python2.7/dist-packages/requests/__init__.py:80: RequestsDependencyWarning: urllib3 (1.23) or chardet (3.0.4) doesn\'t match a supported version!\\r\\r\\n  RequestsDependencyWarning)\\r\\r\\n/usr/local/lib/python2.7/dist-packages/cryptography/hazmat/primitives/constant_time.py:26: CryptographyDeprecationWarning: Support for your Python version is deprecated. The next version of cryptography will remove support. Please upgrade to a 2.7.x release that supports hmac.compare_digest as soon as possible.\\r\\r\\n  utils.DeprecatedIn23,\\r\\r\\nCreating network \\"github-com-oliverkoo-github-checks-buildkite-plugin-v0-0-4_default\\" with the default driver\\r\\r\\nghapp.github.identity Resolved app_id: 26924\\r\\r\\nghapp.github.identity Resolved private key from value.\\r\\r\\nghapp.github.identity Resolved private key.\\r\\r\\nasyncio Using selector: EpollSelector\\r\\r\\nroot job_env: JobEnviron(CI=1, BUILDKITE=1, BUILDKITE_LABEL=\'Plugin Tests\', BUILDKITE_BRANCH=\'add_build_ymal\', BUILDKITE_COMMIT=\'849e85ca66f877081b68d9f41aef934499682154\', BUILDKITE_REPO=\'git@github.com:OliverKoo/simulation.git\', BUILDKITE_BUILD_ID=\'46ed1fa8-904f-4e59-837a-3bd72fdf4d03\', BUILDKITE_BUILD_NUMBER=\'87\', BUILDKITE_BUILD_URL=\'https://buildkite.com/uber-atg/oliver-simulation/builds/87\', BUILDKITE_JOB_ID=\'40871ae4-d820-433e-8dbd-f59b77f5f9f5\', BUILDKITE_COMMAND=\'.buildkite/bin/tests\', BUILDKITE_TIMEOUT=0, BUILDKITE_COMMAND_EXIT_STATUS=1)\\r\\r\\nroot Issuing app jwt: {\'iat\': 1552592455, \'exp\': 1552593055, \'iss\': 26924}\\r\\r\\nghapp.github.checks <ClientResponse(https://api.github.com/repos/OliverKoo/simulation/commits/849e85ca66f877081b68d9f41aef934499682154/check-runs) [200 OK]>\\r\\r\\n<CIMultiDictProxy(\'Server\': \'GitHub.com\', \'Date\': \'Thu, 14 Mar 2019 19:40:56 GMT\', \'Content-Type\': \'application/json; charset=utf-8\', \'Transfer-Encoding\': \'chunked\', \'Status\': \'200 OK\', \'X-RateLimit-Limit\': \'5000\', \'X-RateLimit-Remaining\': \'4931\', \'X-RateLimit-Reset\': \'1552594903\', \'Cache-Control\': \'private, max-age=60, s-maxage=60\', \'Vary\': \'Accept, Authorization, Cookie, X-GitHub-OTP\', \'ETag\': \'W/\\"991f52be025343c254b5acbf51c6702a\\"\', \'X-GitHub-Media-Type\': \'github.antiope-preview; format=json\', \'Access-Control-Expose-Headers\': \'ETag, Link, Location, Retry-After, X-GitHub-OTP, X-RateLimit-Limit, X-RateLimit-Remaining, X-RateLimit-Reset, X-OAuth-Scopes, X-Accepted-OAuth-Scopes, X-Poll-Interval, X-GitHub-Media-Type\', \'Access-Control-Allow-Origin\': \'*\', \'Strict-Transport-Security\': \'max-age=31536000; includeSubdomains; preload\', \'X-Frame-Options\': \'deny\', \'X-Content-Type-Options\': \'nosniff\', \'X-XSS-Protection\': \'1; mode=block\', \'Referrer-Policy\': \'origin-when-cross-origin, strict-origin-when-cross-origin\', \'Content-Security-Policy\': \\"default-src \'none\'\\", \'Content-Encoding\': \'gzip\', \'X-GitHub-Request-Id\': \'D517:7875:60E60B:D0D2A4:5C8AAE48\')>\\r\\r\\n\\r\\r\\nroot current_runs: [RunDetails(name=\'App Tests\', id=\'78435737\', head_sha=\'849e85ca66f877081b68d9f41aef934499682154\', head_branch=None, details_url=\'https://buildkite.com/uber-atg/oliver-simulation/builds/87#ae006e42-7cdb-4881-8089-e589c93a74b5\', external_id=\'ae006e42-7cdb-4881-8089-e589c93a74b5\', status=<Status.in_progress: \'in_progress\'>, started_at=\'2019-03-14T19:40:51Z\', conclusion=None, completed_at=None, output=Output(title=\'None\', summary=\'None\', text=None)), RunDetails(name=\'Plugin Tests\', id=\'78435729\', head_sha=\'849e85ca66f877081b68d9f41aef934499682154\', head_branch=None, details_url=\'https://buildkite.com/uber-atg/oliver-simulation/builds/87#40871ae4-d820-433e-8dbd-f59b77f5f9f5\', external_id=\'40871ae4-d820-433e-8dbd-f59b77f5f9f5\', status=<Status.in_progress: \'in_progress\'>, started_at=\'2019-03-14T19:40:51Z\', conclusion=None, completed_at=None, output=Output(title=\'None\', summary=\'None\', text=None))]\\r\\r\\nroot action: UpdateRun(owner=\'OliverKoo\', repo=\'simulation\', run=RunDetails(name=\'Plugin Tests\', id=\'78435729\', head_sha=None, head_branch=None, details_url=\'https://buildkite.com/uber-atg/oliver-simulation/builds/87#40871ae4-d820-433e-8dbd-f59b77f5f9f5\', external_id=\'40871ae4-d820-433e-8dbd-f59b77f5f9f5\', status=<Status.completed: \'completed\'>, started_at=None, conclusion=<Conclusion.failure: \'failure\'>, completed_at=\'2019-03-14T19:40:56Z\', output=Output(title=\'buildkite/plugin-tests\', summary=\'test_summary.md\', text=None)))\\r\\r\\nghapp.github.checks PATCH https://api.github.com/repos/OliverKoo/simulation/check-runs/78435729\\r\\r\\n{\'name\': \'Plugin Tests\', \'id\': \'78435729\', \'details_url\': \'https://buildkite.com/uber-atg/oliver-simulation/builds/87#40871ae4-d820-433e-8dbd-f59b77f5f9f5\', \'external_id\': \'40871ae4-d820-433e-8dbd-f59b77f5f9f5\', \'status\': \'completed\', \'conclusion\': \'failure\', \'completed_at\': \'2019-03-14T19:40:56Z\', \'output\': {\'title\': \'buildkite/plugin-tests\', \'summary\': \'test_summary.md\'}}\\r\\r\\n+ docker-compose -f /home/rbot/.buildkite-agent/plugins/github-com-OliverKoo-github-checks-buildkite-plugin-v0-0-4/hooks/../docker-compose.yml down\\r\\r\\n/usr/local/lib/python2.7/dist-packages/requests/__init__.py:80: RequestsDependencyWarning: urllib3 (1.23) or chardet (3.0.4) doesn\'t match a supported version!\\r\\r\\n  RequestsDependencyWarning)\\r\\r\\n/usr/local/lib/python2.7/dist-packages/cryptography/hazmat/primitives/constant_time.py:26: CryptographyDeprecationWarning: Support for your Python version is deprecated. The next version of cryptography will remove support. Please upgrade to a 2.7.x release that supports hmac.compare_digest as soon as possible.\\r\\r\\n utils.DeprecatedIn23,\\r\\r\\nRemoving network github-com-oliverkoo-github-checks-buildkite-plugin-v0-0-4_default\\r\\r\\n",\n  "size": 31551\n}\n'