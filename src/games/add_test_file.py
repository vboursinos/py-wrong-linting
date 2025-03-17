import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set

from git import Blob, GitCommandError, InvalidGitRepositoryError, NoSuchPathError, Repo
from loguru import logger

# Source imports
from sourcekon.tools.tool_data_types import AnalysisTool, MessageCategory
from sourcekon.tools.vcs.git.git_tool_dtos import GitExecuteInput, GitExecuteOutput, GitMessage


def create_git_message(current_file: Path, start_line_num: int, end_line_num: int, message: str) -> GitMessage:
    return GitMessage(
        category=MessageCategory.GIT_DIFF,
        file_path=current_file,
        module=None,
        object=None,
        start_line=start_line_num,
        start_column=None,
        end_line=end_line_num,
        end_column=None,
        message_id=None,
        message_name=None,
        message=message,
    )


def create_git_messages(changes_per_file: Dict[str, List[str]], source_path: Optional[Path] = None) -> GitExecuteOutput:
    git_messages = []
    for current_file, changes in changes_per_file.items():
        try:
            git_message = GitMessage(
                category=MessageCategory.GIT_DIFF,
                file_path=Path(current_file),
                module=None,
                object=None,
                start_line=1,
                start_column=None,
                end_line=get_file_lines_count(current_file, source_path),
                end_column=None,
                message_id=None,
                message_name=None,
                message=None,
                context={"changes": changes},
            )
            git_messages.append(git_message)
        except (InvalidGitRepositoryError, GitCommandError, NoSuchPathError) as e:
            logger.warning(f"Git error: {current_file} - {e}")
        except (KeyError, AttributeError, TypeError, UnicodeDecodeError, OSError) as e:
            logger.warning(f"Operation error: {current_file} - {e}")

    return GitExecuteOutput(messages=git_messages, output_file_path=Path(), tool=AnalysisTool.GIT)


def handle_cloning(data: GitExecuteInput):
    if data.git_url and (not os.path.exists(str(data.source_path)) or not os.listdir(str(data.source_path))):
        logger.info(f"Cloning repository from {data.git_url} to {data.source_path}")
        clone_repo(data.git_url, str(data.source_path))
    elif data.git_url:
        logger.info(f"Directory {data.source_path} already exists and is not empty. Skipping clone.")


def clone_repo(remote_url: str, local_dir: str):
    Repo.clone_from(remote_url, local_dir)


def get_diffs(repo, main_branch: str, new_branch: str):
    commit_branch1 = repo.commit(main_branch)
    commit_branch2 = repo.commit(new_branch)
    return repo.git.diff(f"{commit_branch1}..{commit_branch2}").split("\n")


def process_blob(
    repo: Repo,
    obj: Blob,
    user_email: Optional[str],
    start_date: Optional[datetime],
    end_date: Optional[datetime],
    user_lines: Dict[str, Dict[str, Dict[str, Set]]],
):
    file_path = str(obj.path)
    blame = repo.blame_incremental("HEAD", file_path)

    for final_blob in blame:
        commit_date = final_blob.commit.committed_datetime.replace(tzinfo=None)
        author_email = final_blob.commit.author.email

        if (
            (start_date and commit_date < start_date)
            or (end_date and commit_date > end_date)
            or (user_email and author_email != user_email)
        ):
            continue

        if file_path not in user_lines:
            user_lines[file_path] = {}

        if author_email not in user_lines[file_path]:
            user_lines[file_path][author_email] = {
                "linenos": set(),
                "commit_dates": set(),
            }

        author_info = user_lines[file_path][author_email]
        author_info["linenos"].update(final_blob.linenos)
        author_info["commit_dates"].add(commit_date.strftime("%Y-%m-%d %H:%M:%S"))


def get_user_lines_in_repo(
    repo: Repo, user_email: Optional[str], start_date: Optional[datetime] = None, end_date: Optional[datetime] = None
) -> Dict[str, Dict[str, Dict[str, Set]]]:
    user_lines: Dict[str, Dict[str, Dict[str, Set]]] = {}

    if start_date and start_date.tzinfo is not None:
        start_date = start_date.replace(tzinfo=None)

    if end_date and end_date.tzinfo is not None:
        end_date = end_date.replace(tzinfo=None)

    for obj in repo.tree().traverse():
        if isinstance(obj, Blob):
            try:
                process_blob(repo, obj, user_email, start_date, end_date, user_lines)
            except GitCommandError:
                continue
    print(user_lines)
    return user_lines


def construct_messages_from_user_lines(user_lines: Dict[str, Dict[str, Dict[str, Set]]]) -> List[GitMessage]:
    messages = []

    for filepath, author_info in user_lines.items():
        for author_email, fileinfo in author_info.items():
            sorted_linenos: List[int] = sorted(int(lineno) for lineno in fileinfo["linenos"] if isinstance(lineno, int))
            if not sorted_linenos:
                continue

            commit_dates = list(fileinfo["commit_dates"])
            if not commit_dates:
                continue

            message_info = f"{commit_dates[0]} - Author: {author_email}"

            ranges = [{"start_line": sorted_linenos[0], "end_line": sorted_linenos[0], "message": message_info}]

            for lineno in sorted_linenos[1:]:
                if (
                    isinstance(lineno, int)
                    and isinstance(ranges[-1]["end_line"], int)
                    and lineno - ranges[-1]["end_line"] == 1
                ):
                    ranges[-1]["end_line"] = lineno
                else:
                    ranges.append({"start_line": lineno, "end_line": lineno, "message": message_info})

            for change in ranges:
                start_line = change["start_line"]
                end_line = change["end_line"]

                if isinstance(start_line, (int, float)) and isinstance(end_line, (int, float)):
                    git_message: GitMessage = create_git_message(
                        Path(filepath), int(start_line), int(end_line), str(change["message"])
                    )
                    messages.append(git_message)
    print(messages)
    return messages


def run_command(command: List[str], working_dir: Optional[str], ignore_errors: bool = False) -> str:
    try:
        result = subprocess.run(command, cwd=working_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        return result.stdout.decode("utf-8")
    except subprocess.CalledProcessError as e:
        if ignore_errors:
            return e.stdout.decode("utf-8")
        raise RuntimeError(f"Command {command} failed with error: {e.stderr.decode('utf-8')}") from e


def branch_exists(branch_name: Optional[str], working_dir: Optional[str]) -> bool:
    if not branch_name:
        return False
    # Get both local and remote branches
    branches = run_command(["git", "branch", "--list"], working_dir, ignore_errors=True)
    branches += run_command(["git", "branch", "-r"], working_dir, ignore_errors=True)
    return branch_name in branches.split()


def parse_diffs_by_file(diffs: List[str]) -> Dict[str, List[str]]:
    file_changes: Dict = {}
    current_file = None

    for line in diffs:
        if line.startswith("diff --git"):
            parts = line.split(" ")
            if len(parts) >= 3:
                current_file = parts[2][2:]  # Remove 'a/' prefix
                file_changes[current_file] = []
        elif current_file and not line.startswith("---") and not line.startswith("+++"):
            file_changes[current_file].append(line)

    return file_changes


def change_branch(branch_name: Optional[str], working_dir: Optional[str]):
    """
    Change to a specified branch in the repository. If the branch does not exist, it will be created.

    Args:
        branch_name (str): The name of the branch to switch to.
        working_dir (Optional[str]): The working directory of the Git repository.
    """
    if branch_name is None:
        raise ValueError("Branch must not be None")
    if branch_exists(branch_name, working_dir):
        logger.info(f"Switching to existing branch '{branch_name}'.")
        run_command(["git", "checkout", branch_name], working_dir)
    else:
        logger.info(f"Branch '{branch_name}' does not exist. Creating and switching to it.")
        run_command(["git", "checkout", "-b", branch_name], working_dir)


def get_file_lines_count(file_path, source_path):
    """Get the number of lines in a file from the current HEAD of the repository."""
    source_path = source_path if source_path is not None else "."
    repo = Repo(source_path)
    file_path = Path(file_path).as_posix()
    blob = repo.head.commit.tree / file_path
    content = blob.data_stream.read().decode("utf-8")
    return len(content.splitlines())
