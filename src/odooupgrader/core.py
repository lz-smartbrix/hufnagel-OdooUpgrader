import os
import shutil
import subprocess
import sys
import threading
import time
import zipfile
import logging
from typing import Optional, List

import requests
from packaging import version
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn, TimeElapsedColumn

console = Console()
logger = logging.getLogger("odooupgrader")


class OdooUpgrader:
    VALID_VERSIONS = ["10.0", "11.0", "12.0", "13.0", "14.0", "15.0", "16.0", "17.0", "18.0", "19.0"]

    def __init__(self, source: str, target_version: str, extra_addons: Optional[str] = None, verbose: bool = False,
                 postgres_version: str = "13", defer_extra_addons: bool = False):
        self.source = source
        self.target_version = target_version
        self.extra_addons = extra_addons
        self.verbose = verbose
        self.postgres_version = postgres_version
        self.defer_extra_addons = defer_extra_addons
        self.cwd = os.getcwd()
        self.source_dir = os.path.join(self.cwd, 'source')
        self.output_dir = os.path.join(self.cwd, 'output')
        self.filestore_dir = os.path.join(self.output_dir, 'filestore')
        self.custom_addons_dir = os.path.join(self.output_dir, 'custom_addons')
        self.compose_cmd = self._get_docker_compose_cmd()

    def _run_cmd(self, cmd: List[str], check: bool = True, capture_output: bool = False) -> subprocess.CompletedProcess:
        """Executes a subprocess command and logs it."""
        cmd_str = " ".join(cmd)
        logger.debug(f"Executing: {cmd_str}")

        try:
            result = subprocess.run(
                cmd,
                check=check,
                text=True,
                capture_output=capture_output
            )
            if capture_output and result.stdout:
                logger.debug(f"Command Output: {result.stdout.strip()}")
            return result
        except subprocess.CalledProcessError as e:
            logger.error(f"Command failed: {cmd_str}")
            if capture_output and e.stderr:
                logger.error(f"Error output: {e.stderr.strip()}")
            raise

    def _get_docker_compose_cmd(self) -> List[str]:
        """Determines if 'docker compose' or 'docker-compose' is available."""
        try:
            subprocess.run(["docker", "compose", "version"], check=True, capture_output=True)
            return ["docker", "compose"]
        except (subprocess.CalledProcessError, FileNotFoundError):
            try:
                subprocess.run(["docker-compose", "--version"], check=True, capture_output=True)
                return ["docker-compose"]
            except (subprocess.CalledProcessError, FileNotFoundError):
                return ["docker", "compose"]

    def _cleanup_dir(self, path: str):
        """Safely removes a directory."""
        if os.path.exists(path):
            try:
                shutil.rmtree(path)
                logger.debug(f"Removed directory: {path}")
            except Exception as e:
                msg = f"Warning: Could not remove {path}: {e}"
                console.print(f"[yellow]{msg}[/yellow]")
                logger.warning(msg)

    def validate_source_accessibility(self):
        """Checks if source file or URL is valid using Requests."""
        console.print("[blue]Validating source accessibility...[/blue]")
        logger.info(f"Validating source: {self.source}")

        if self.source.startswith("http://") or self.source.startswith("https://"):
            try:
                with requests.get(self.source, stream=True, timeout=30) as response:
                    response.raise_for_status()
                console.print("[green]Source URL is accessible.[/green]")
            except requests.RequestException as e:
                console.print(f"[bold red]Error:[/bold red] Source URL is not accessible: {e}")
                logger.error(f"Source URL invalid: {e}")
                sys.exit(1)
        else:
            if not os.path.exists(self.source):
                console.print(f"[bold red]Error:[/bold red] Source file not found: {self.source}")
                logger.error(f"Source file not found: {self.source}")
                sys.exit(1)
            console.print("[green]Source file exists.[/green]")

        if self.extra_addons:
            console.print("[blue]Validating extra addons...[/blue]")
            if "://" in self.extra_addons:
                if not (self.extra_addons.startswith("http://") or self.extra_addons.startswith("https://")):
                    console.print(
                        f"[bold red]Error:[/bold red] Invalid protocol for addons URL. Only http/https supported.")
                    sys.exit(1)

                try:
                    with requests.head(self.extra_addons, timeout=30, allow_redirects=True) as response:
                        if response.status_code >= 400:
                            raise requests.RequestException("Status code error")
                except requests.RequestException:
                    console.print(f"[bold red]Error:[/bold red] Extra addons URL is not accessible.")
                    logger.error("Extra addons URL invalid")
                    sys.exit(1)
            else:
                if not os.path.exists(self.extra_addons):
                    console.print(f"[bold red]Error:[/bold red] Extra addons path not found: {self.extra_addons}")
                    logger.error(f"Extra addons path not found: {self.extra_addons}")
                    sys.exit(1)

    def prepare_environment(self):
        """Creates necessary directories and cleans old data."""
        logger.info("Preparing environment directories...")
        self._cleanup_dir(self.source_dir)
        self._cleanup_dir(self.output_dir)

        os.makedirs(self.source_dir, exist_ok=True)
        os.makedirs(self.filestore_dir, exist_ok=True)
        os.makedirs(self.custom_addons_dir, exist_ok=True)

        if sys.platform != "win32":
            try:
                os.chmod(self.output_dir, 0o777)
                for root, dirs, files in os.walk(self.output_dir):
                    for d in dirs:
                        os.chmod(os.path.join(root, d), 0o777)
                    for f in files:
                        os.chmod(os.path.join(root, f), 0o777)
            except Exception as e:
                logger.warning(f"Could not set broad permissions on output dir: {e}")

    def download_file(self, url: str, dest_path: str, description: str = "Downloading..."):
        """Generic download helper."""
        logger.info(f"Downloading {url} to {dest_path}")
        try:
            with requests.get(url, stream=True, timeout=60) as response:
                response.raise_for_status()
                total_size = int(response.headers.get("Content-Length", 0))

                with Progress(
                        SpinnerColumn(),
                        TextColumn("[progress.description]{task.description}"),
                        BarColumn(),
                        TaskProgressColumn(),
                        "•",
                        TimeElapsedColumn(),
                        console=console
                ) as progress:
                    task = progress.add_task(f"[cyan]{description}", total=total_size)
                    with open(dest_path, "wb") as f:
                        for chunk in response.iter_content(chunk_size=8192):
                            f.write(chunk)
                            progress.update(task, advance=len(chunk))
        except requests.RequestException as e:
            console.print(f"[bold red]Download failed:[/bold red] {e}")
            logger.error(f"Download failed: {e}")
            sys.exit(1)

    def download_or_copy_source(self) -> str:
        """Downloads file from URL using Requests with Rich progress bar."""
        target_path = ""
        if self.source.startswith("http://") or self.source.startswith("https://"):
            filename = os.path.basename(self.source.split("?")[0]) or "downloaded_db.dump"
            target_path = os.path.join(self.cwd, filename)
            self.download_file(self.source, target_path, "Downloading source DB...")
        else:
            target_path = self.source
        return target_path

    def process_extra_addons(self):
        """Downloads, copies, and extracts extra addons to output/custom_addons."""
        if not self.extra_addons:
            return

        console.print("[blue]Processing custom addons...[/blue]")
        logger.info("Processing custom addons...")

        if self.extra_addons.startswith("http://") or self.extra_addons.startswith("https://"):
            zip_path = os.path.join(self.source_dir, "addons.zip")
            self.download_file(self.extra_addons, zip_path, "Downloading extra addons...")
            try:
                with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                    zip_ref.extractall(self.custom_addons_dir)
                os.remove(zip_path)
            except zipfile.BadZipFile:
                console.print("[bold red]Error:[/bold red] Downloaded addons file is not a valid zip.")
                logger.error("Invalid addons zip file")
                sys.exit(1)

        elif os.path.isfile(self.extra_addons) and self.extra_addons.lower().endswith('.zip'):
            try:
                with zipfile.ZipFile(self.extra_addons, 'r') as zip_ref:
                    zip_ref.extractall(self.custom_addons_dir)
            except zipfile.BadZipFile:
                console.print("[bold red]Error:[/bold red] Addons file is not a valid zip.")
                sys.exit(1)

        elif os.path.isdir(self.extra_addons):
            try:
                shutil.copytree(self.extra_addons, self.custom_addons_dir, dirs_exist_ok=True)
            except Exception as e:
                console.print(f"[bold red]Error:[/bold red] Failed to copy local addons: {e}")
                sys.exit(1)

        # Handle directory nesting (common in GitHub downloads), ignoring hidden files
        items = [i for i in os.listdir(self.custom_addons_dir) if not i.startswith('.')]
        if len(items) == 1:
            single_item_path = os.path.join(self.custom_addons_dir, items[0])
            if os.path.isdir(single_item_path):
                sub_items = os.listdir(single_item_path)
                is_module = any(x in sub_items for x in ['__manifest__.py', '__openerp__.py'])

                if not is_module:
                    logger.info(f"Detected wrapper directory '{items[0]}'. Flattening structure...")
                    for sub in sub_items:
                        src_sub = os.path.join(single_item_path, sub)
                        dst_sub = os.path.join(self.custom_addons_dir, sub)
                        if not os.path.exists(dst_sub):
                            shutil.move(src_sub, dst_sub)
                    try:
                        os.rmdir(single_item_path)
                    except OSError:
                        pass

        # Check for flat structure (single module at root)
        items = os.listdir(self.custom_addons_dir)
        has_manifest = any(x in items for x in ['__manifest__.py', '__openerp__.py'])

        if has_manifest:
            logger.info("Detected flat addon structure. Reorganizing...")
            sub_folder_name = "downloaded_module"
            sub_folder_path = os.path.join(self.custom_addons_dir, sub_folder_name)
            os.makedirs(sub_folder_path, exist_ok=True)

            for item in items:
                src = os.path.join(self.custom_addons_dir, item)
                dst = os.path.join(sub_folder_path, item)
                if src != sub_folder_path:
                    shutil.move(src, dst)

        # Ensure requirements.txt exists
        req_path = os.path.join(self.custom_addons_dir, "requirements.txt")
        if not os.path.exists(req_path):
            with open(req_path, "w") as f:
                f.write("")
        elif os.path.getsize(req_path) == 0:
            logger.warning("Empty requirements.txt found in custom addons.")

        # Standardize permissions for Docker compatibility
        logger.info("Standardizing addon permissions...")
        for root, dirs, files in os.walk(self.custom_addons_dir):
            for d in dirs:
                try:
                    os.chmod(os.path.join(root, d), 0o755)
                except Exception:
                    pass
            for f in files:
                try:
                    if f.endswith('.sh'):
                        os.chmod(os.path.join(root, f), 0o755)
                    else:
                        os.chmod(os.path.join(root, f), 0o644)
                except Exception:
                    pass

        console.print("[green]Custom addons prepared.[/green]")

    def _list_custom_modules(self) -> List[str]:
        """Scans the custom_addons_dir for valid Odoo modules."""
        if not self.extra_addons or not os.path.exists(self.custom_addons_dir):
            return []

        modules = []
        for item in os.listdir(self.custom_addons_dir):
            item_path = os.path.join(self.custom_addons_dir, item)
            if os.path.isdir(item_path):
                if (os.path.exists(os.path.join(item_path, '__manifest__.py')) or
                        os.path.exists(os.path.join(item_path, '__openerp__.py'))):
                    modules.append(item)
        return modules

    def _get_custom_module_names(self) -> str:
        """Returns a comma-separated string of custom module names for --load."""
        modules = self._list_custom_modules()
        if modules:
            return "," + ",".join(modules)
        return ""

    def _defer_custom_modules(self):
        """Marks custom modules as 'to install' so they are skipped during upgrade without data loss."""
        modules = self._list_custom_modules()
        if not modules:
            return

        names_sql = ",".join(f"'{name}'" for name in modules)
        query = (
            f"UPDATE ir_module_module SET state='to install' "
            f"WHERE name IN ({names_sql}) "
            f"AND state IN ('installed', 'to upgrade', 'to remove');"
        )
        cmd = ["docker", "exec", "-i", "db-odooupgrade", "psql", "-U", "odoo", "-d", "database", "-c", query]
        self._run_cmd(cmd, check=False, capture_output=True)
        console.print(
            f"[yellow]Deferred custom modules (data preserved, reinstall after upgrade): "
            f"{', '.join(modules)}[/yellow]"
        )
        logger.info(f"Deferred custom modules: {modules}")

    def _get_update_modules_arg(self, exclude_modules: List[str]) -> str:
        """Returns comma-separated module names to update, excluding deferred custom modules."""
        if not exclude_modules:
            return "all"

        names_sql = ",".join(f"'{name}'" for name in exclude_modules)
        query = (
            f"SELECT name FROM ir_module_module "
            f"WHERE state IN ('installed', 'to upgrade') "
            f"AND name NOT IN ({names_sql}) "
            f"ORDER BY name;"
        )
        cmd = ["docker", "exec", "-i", "db-odooupgrade", "psql", "-U", "odoo", "-d", "database", "-t", "-A", "-c", query]
        res = self._run_cmd(cmd, check=False, capture_output=True)
        modules = [line.strip() for line in res.stdout.splitlines() if line.strip()]
        return ",".join(modules) if modules else "base"

    def process_source_file(self, filepath: str) -> str:
        """Extracts ZIP or prepares DUMP file."""
        ext = os.path.splitext(filepath)[1].lower()

        if ext == '.zip':
            console.print("[blue]Extracting ZIP file...[/blue]")
            logger.info("Extracting ZIP file...")
            with zipfile.ZipFile(filepath, 'r') as zip_ref:
                zip_ref.extractall(self.source_dir)
            return "ZIP"
        else:
            console.print("[blue]Processing DUMP file...[/blue]")
            logger.info("Processing DUMP file...")
            shutil.copy2(filepath, os.path.join(self.source_dir, "database.dump"))
            return "DUMP"

    def create_db_compose_file(self):
        """Generates the docker-compose file for the database."""
        content = f"""
services:
  db-odooupgrade:
    container_name: db-odooupgrade
    image: postgres:{self.postgres_version}
    environment:
      - POSTGRES_DB=odoo
      - POSTGRES_PASSWORD=odoo
      - POSTGRES_USER=odoo
    networks:
      - odooupgrade-connection
    volumes:
      - postgres_data:/var/lib/postgresql/data
    restart: unless-stopped

networks:
  odooupgrade-connection:
    driver: bridge
    name: odooupgrade-connection

volumes:
  postgres_data:
"""
        with open("db-composer.yml", "w", newline='\n') as f:
            f.write(content.strip())

    def wait_for_db(self):
        """Waits until Postgres is ready."""
        console.print("[yellow]Waiting for database to be ready...[/yellow]")
        max_retries = 30
        cmd = ["docker", "exec", "db-odooupgrade", "pg_isready", "-U", "odoo", "-d", "odoo"]

        for _ in range(max_retries):
            try:
                subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                console.print("[green]Database is ready.[/green]")
                return
            except subprocess.CalledProcessError:
                time.sleep(2)

        console.print("[bold red]Database failed to start.[/bold red]")
        sys.exit(1)

    def restore_database(self, file_type: str):
        """Restores the database dump."""
        console.print("[blue]Restoring database...[/blue]")
        logger.info("Restoring database...")

        self._run_cmd(["docker", "exec", "db-odooupgrade", "createdb", "-U", "odoo", "database"], check=False)

        if file_type == "ZIP":
            dump_path = os.path.join(self.source_dir, "dump.sql")
            if not os.path.exists(dump_path):
                found_sql = [f for f in os.listdir(self.source_dir) if f.endswith('.sql')]
                if found_sql:
                    dump_path = os.path.join(self.source_dir, found_sql[0])
                else:
                    console.print("[bold red]No dump.sql found inside ZIP.[/bold red]")
                    sys.exit(1)

            src_filestore = os.path.join(self.source_dir, "filestore")
            if os.path.exists(src_filestore):
                try:
                    shutil.copytree(src_filestore, self.filestore_dir, dirs_exist_ok=True)
                    if sys.platform != "win32":
                        try:
                            os.chmod(self.filestore_dir, 0o777)
                            for root, dirs, files in os.walk(self.filestore_dir):
                                for d in dirs:
                                    os.chmod(os.path.join(root, d), 0o777)
                                for f in files:
                                    os.chmod(os.path.join(root, f), 0o777)
                        except Exception as e:
                            logger.warning(f"Failed to set filestore permissions: {e}")
                except Exception as e:
                    logger.warning(f"Failed to copy filestore: {e}")

            self._fix_filestore_permissions()

            self._run_cmd(["docker", "cp", dump_path, "db-odooupgrade:/tmp/dump.sql"])
            self._run_cmd(["docker", "exec", "-i", "db-odooupgrade", "psql", "-U", "odoo", "-d", "database", "-f",
                           "/tmp/dump.sql"], capture_output=True)

        elif file_type == "DUMP":
            dump_path = os.path.join(self.source_dir, "database.dump")
            self._run_cmd(["docker", "cp", dump_path, "db-odooupgrade:/tmp/database.dump"])

            cmd = [
                "docker", "exec", "db-odooupgrade", "pg_restore",
                "-U", "odoo", "-d", "database",
                "--no-owner", "--no-privileges", "--clean", "--if-exists",
                "--disable-triggers", "--single-transaction",
                "/tmp/database.dump"
            ]
            self._run_cmd(cmd, check=False)

    def get_current_version(self) -> str:
        """Queries the database to find the current Odoo version."""
        queries = [
            "SELECT latest_version FROM ir_module_module WHERE name = 'base' AND state = 'installed';",
            "SELECT value FROM ir_config_parameter WHERE key = 'database.latest_version';",
            "SELECT latest_version FROM ir_module_module WHERE name = 'base' ORDER BY id DESC LIMIT 1;"
        ]

        for q in queries:
            cmd = ["docker", "exec", "-i", "db-odooupgrade", "psql", "-U", "odoo", "-d", "database", "-t", "-A", "-c",
                   q]
            try:
                res = self._run_cmd(cmd, check=False, capture_output=True)
                ver = res.stdout.strip()
                if ver:
                    return ver
            except Exception:
                continue
        return ""

    def get_version_info(self, ver_str: str) -> version.Version:
        """Parses version string securely using packaging.version."""
        try:
            clean_ver = ver_str.strip()
            return version.parse(clean_ver)
        except Exception:
            return version.parse("0.0")

    def generate_next_version(self, current: str) -> str:
        """Calculates next major version (e.g. 15.0 -> 16.0)."""
        try:
            major = int(current.split('.')[0])
            return f"{major + 1}.0"
        except Exception:
            v = version.parse(current)
            return f"{v.major + 1}.0"

    def _get_major_version(self, ver_str: str) -> int:
        """Returns the major Odoo version number from a version string."""
        return self.get_version_info(ver_str).major

    def _image_tag_for_version(self, target_version: str) -> str:
        """Returns a unique Docker image tag per upgrade step."""
        return f"odoo-openupgrade-{target_version.replace('.', '-')}"

    def _monitor_odoo_log(self, stop_event: threading.Event):
        """Prints the latest odoo.log lines while an upgrade container is running."""
        log_path = os.path.join(self.output_dir, "odoo.log")
        last_pos = 0
        last_printed = ""

        while not stop_event.is_set():
            if os.path.exists(log_path):
                try:
                    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                        f.seek(last_pos)
                        new_content = f.read()
                        if new_content:
                            last_pos = f.tell()
                            lines = [line.strip() for line in new_content.strip().split("\n") if line.strip()]
                            if lines:
                                latest = lines[-1]
                                if latest != last_printed:
                                    console.print(f"[dim]odoo.log:[/dim] {latest}")
                                    last_printed = latest
                except Exception:
                    pass
            stop_event.wait(20)

    def _fix_filestore_permissions(self):
        """Ensure the filestore bind mount is writable inside Docker (especially on Windows)."""
        if not os.path.exists(self.filestore_dir):
            return

        if sys.platform != "win32":
            try:
                os.chmod(self.filestore_dir, 0o777)
                for root, dirs, files in os.walk(self.filestore_dir):
                    for d in dirs:
                        os.chmod(os.path.join(root, d), 0o777)
                    for f in files:
                        os.chmod(os.path.join(root, f), 0o666)
            except Exception as e:
                logger.warning(f"Host-side filestore chmod failed: {e}")

        filestore_abs = os.path.abspath(self.filestore_dir)
        self._run_cmd(
            [
                "docker", "run", "--rm",
                "-v", f"{filestore_abs}:/data",
                "alpine:3",
                "sh", "-c", "chmod -R 777 /data",
            ],
            check=False,
        )

    def run_upgrade_step(self, target_version: str) -> bool:
        """Builds and runs the OpenUpgrade container."""
        logger.info(f"Preparing upgrade step to version {target_version}")

        is_final_step = (target_version == self.target_version)

        extra_addons_cmds = ""
        extra_addons_path_arg = ""
        custom_modules_load = ""
        update_modules_arg = "all"
        deferred_modules: List[str] = []

        if self.extra_addons:
            if is_final_step and self.defer_extra_addons:
                deferred_modules = self._list_custom_modules()
                self._defer_custom_modules()
                update_modules_arg = self._get_update_modules_arg(deferred_modules)
                logger.info(
                    f"Skipping custom addons on final upgrade step (defer-extra-addons). "
                    f"Updating {update_modules_arg.count(',') + 1} core modules."
                )
            else:
                # Force cache invalidation so COPY instructions run every time
                with open(os.path.join(self.custom_addons_dir, ".build_timestamp"), "w") as f:
                    f.write(str(time.time()))

                # Optimized Layering: Copy reqs first, then pip, then code
                extra_addons_cmds = """
RUN mkdir -p /mnt/custom-addons
COPY --chown=odoo:odoo ./output/custom_addons/requirements.txt /mnt/custom-addons/requirements.txt
RUN if pip3 install --help | grep -q -- '--break-system-packages'; then \
        pip3 install --break-system-packages --no-cache-dir -r /mnt/custom-addons/requirements.txt; \
    else \
        pip3 install --no-cache-dir -r /mnt/custom-addons/requirements.txt; \
    fi
COPY --chown=odoo:odoo ./output/custom_addons/ /mnt/custom-addons/
"""

                if is_final_step:
                    logger.info("Target version reached. Injecting custom addons path and modules.")
                    extra_addons_path_arg = ",/mnt/custom-addons"
                    custom_modules_load = self._get_custom_module_names()
                else:
                    logger.info("Intermediate version. Skipping custom addons loading.")

        dockerfile_content = f"""
FROM odoo:{target_version}
USER root
RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*
RUN git clone https://github.com/OCA/OpenUpgrade.git --depth 1 --branch {target_version} /mnt/extra-addons
RUN if pip3 install --help | grep -q -- '--break-system-packages'; then \
        pip3 install --break-system-packages --no-cache-dir -r /mnt/extra-addons/requirements.txt; \
    else \
        pip3 install --no-cache-dir -r /mnt/extra-addons/requirements.txt; \
    fi

{extra_addons_cmds}
"""
        with open("Dockerfile", "w", newline='\n') as f:
            f.write(dockerfile_content.strip())

        self._fix_filestore_permissions()

        image_tag = self._image_tag_for_version(target_version)

        compose_content = f"""
services:
  odoo-openupgrade:
    image: {image_tag}
    build:
      context: .
      dockerfile: Dockerfile
    container_name: odoo-openupgrade
    user: "0:0"
    environment:
      - HOST=db-odooupgrade
      - POSTGRES_USER=odoo
      - POSTGRES_PASSWORD=odoo
    networks:
      - odooupgrade-connection
    volumes:
      - ./output/filestore:/var/lib/odoo/filestore/database
      - ./output:/var/log/odoo
    restart: "no"
    entrypoint: /entrypoint.sh
    command: >
      odoo -d database
      --upgrade-path=/mnt/extra-addons/openupgrade_scripts/scripts
      --addons-path=/mnt/extra-addons{extra_addons_path_arg}
      --update {update_modules_arg}
      --stop-after-init
      --load=base,web,openupgrade_framework{custom_modules_load}
      --log-level=info
      --logfile=/var/log/odoo/odoo.log
networks:
  odooupgrade-connection:
    external: true
    name: odooupgrade-connection
"""
        with open("odoo-upgrade-composer.yml", "w", newline='\n') as f:
            f.write(compose_content.strip())

        self._run_cmd(["docker", "rm", "-f", "odoo-openupgrade"], check=False, capture_output=True)

        cmd_up = self.compose_cmd + ["-f", "odoo-upgrade-composer.yml", "up", "--build", "--abort-on-container-exit"]

        log_stop = threading.Event()
        log_monitor = threading.Thread(target=self._monitor_odoo_log, args=(log_stop,), daemon=True)
        log_monitor.start()
        compose_returncode = None

        with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                TimeElapsedColumn(),
                console=console
        ) as progress:
            task = progress.add_task(f"[bold magenta]Upgrading to {target_version}...", total=None)
            console.print(
                f"[dim]Monitor progress in {os.path.join(self.output_dir, 'odoo.log')} "
                f"(large databases can take hours)[/dim]"
            )

            try:
                process = subprocess.Popen(
                    cmd_up,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    universal_newlines=True
                )

                while True:
                    output = process.stdout.readline()
                    if output == '' and process.poll() is not None:
                        break
                    if output:
                        line = output.strip()
                        if self.verbose:
                            console.print(f"[dim]{line}[/dim]")
                        logger.debug(line)

                compose_returncode = process.returncode
                if compose_returncode != 0:
                    console.print("[bold red]Upgrade process failed.[/bold red]")
                    logger.error(f"Upgrade process returned non-zero exit code: {compose_returncode}")
                    return False

            except Exception as e:
                console.print(f"[bold red]Error running upgrade:[/bold red] {e}")
                logger.error(f"Exception during upgrade subprocess: {e}")
                return False
            finally:
                log_stop.set()
                log_monitor.join(timeout=1)

        try:
            res = self._run_cmd(
                ["docker", "inspect", "odoo-openupgrade", "--format={{.State.ExitCode}}"],
                check=False,
                capture_output=True,
            )
            if res.returncode == 0:
                exit_code = int(res.stdout.strip())
                logger.info(f"Container exit code: {exit_code}")
                if exit_code != 0:
                    console.print(f"[bold red]Container exited with code {exit_code}[/bold red]")
                    return False
            else:
                logger.warning("Could not inspect container; trusting docker compose exit code.")
        except Exception as e:
            logger.warning(f"Error checking container exit code: {e}")

        console.print(f"[green]Upgrade to {target_version} successful.[/green]")
        self._run_cmd(self.compose_cmd + ["-f", "odoo-upgrade-composer.yml", "down"], check=False)
        return True

    def finalize_package(self):
        """Dumps final database and zips it."""
        console.print("[blue]Creating final package...[/blue]")
        logger.info("Creating final package...")

        dump_cmd = ["docker", "exec", "db-odooupgrade", "pg_dump", "-U", "odoo", "database"]
        try:
            with open(os.path.join(self.output_dir, "dump.sql"), "w") as f:
                subprocess.run(dump_cmd, stdout=f, check=True)
        except Exception as e:
            logger.error(f"Failed to dump database: {e}")
            raise

        zip_name = os.path.join(self.output_dir, "upgraded.zip")
        with zipfile.ZipFile(zip_name, 'w', zipfile.ZIP_DEFLATED) as zipf:
            zipf.write(os.path.join(self.output_dir, "dump.sql"), "dump.sql")

            if os.path.exists(self.filestore_dir):
                for root, _, files in os.walk(self.filestore_dir):
                    for file in files:
                        file_path = os.path.join(root, file)
                        arcname = os.path.relpath(file_path, self.output_dir)
                        zipf.write(file_path, arcname)

        console.print(f"[bold green]Upgrade Complete! Package available at: {zip_name}[/bold green]")
        logger.info(f"Upgrade Complete. Package: {zip_name}")
        os.remove(os.path.join(self.output_dir, "dump.sql"))

    def cleanup_artifacts(self):
        """Removes source folder and extracted filestore."""
        logger.info("Cleaning up artifacts...")
        self._cleanup_dir(self.source_dir)
        self._cleanup_dir(self.filestore_dir)
        self._cleanup_dir(self.custom_addons_dir)

    def cleanup(self):
        """Docker cleanup."""
        console.print("[dim]Cleaning up Docker environment...[/dim]")
        logger.info("Cleaning up Docker environment...")
        if os.path.exists("db-composer.yml"):
            self._run_cmd(self.compose_cmd + ["-f", "db-composer.yml", "down", "-v"], check=False)

        for f in ["Dockerfile", "odoo-upgrade-composer.yml", "db-composer.yml"]:
            if os.path.exists(f):
                os.remove(f)

    def run(self):
        try:
            logger.info("Starting OdooUpgrader...")
            if self.target_version not in self.VALID_VERSIONS:
                console.print(f"[bold red]Invalid version. Supported: {self.VALID_VERSIONS}[/bold red]")
                sys.exit(1)

            self.validate_source_accessibility()
            self.prepare_environment()
            self.process_extra_addons()

            self.create_db_compose_file()

            self._run_cmd(self.compose_cmd + ["-f", "db-composer.yml", "up", "-d"])
            self.wait_for_db()

            local_source = self.download_or_copy_source()
            file_type = self.process_source_file(local_source)

            if local_source != self.source and os.path.exists(local_source):
                os.remove(local_source)

            self.restore_database(file_type)

            current_ver_str = self.get_current_version()
            if not current_ver_str:
                console.print("[bold red]Could not determine database version.[/bold red]")
                logger.error("Could not determine database version")
                sys.exit(1)

            console.print(f"[bold blue]Current Database Version: {current_ver_str}[/bold blue]")
            logger.info(f"Current Database Version: {current_ver_str}")

            current_ver = self.get_version_info(current_ver_str)
            target_ver = self.get_version_info(self.target_version)
            min_ver = self.get_version_info("10.0")

            if current_ver < min_ver:
                console.print("[bold red]Source database version is below 10.0. Not supported.[/bold red]")
                sys.exit(1)

            while True:
                current_ver = self.get_version_info(current_ver_str)

                if current_ver.major == target_ver.major:
                    console.print("[green]Target version reached![/green]")
                    self.finalize_package()
                    self.cleanup_artifacts()
                    break
                elif current_ver.major > target_ver.major:
                    console.print("[yellow]Current version is already higher than target.[/yellow]")
                    self.finalize_package()
                    self.cleanup_artifacts()
                    break
                else:
                    next_ver_str = self.generate_next_version(current_ver_str)
                    console.print(
                        f"[bold cyan]Starting upgrade step: "
                        f"{current_ver_str} → {next_ver_str} (target: {self.target_version})[/bold cyan]"
                    )
                    logger.info(f"Upgrade step: {current_ver_str} -> {next_ver_str}")

                    prev_major = self._get_major_version(current_ver_str)

                    if not self.run_upgrade_step(next_ver_str):
                        console.print("[bold red]Aborting sequence.[/bold red]")
                        sys.exit(1)

                    current_ver_str = self.get_current_version()
                    if not current_ver_str:
                        console.print("[bold red]Could not determine database version after upgrade.[/bold red]")
                        logger.error("Could not determine database version after upgrade")
                        sys.exit(1)

                    new_major = self._get_major_version(current_ver_str)
                    if new_major <= prev_major:
                        console.print(
                            f"[bold red]Upgrade to {next_ver_str} finished but database is still at "
                            f"{current_ver_str}. Check {os.path.join(self.output_dir, 'odoo.log')} for errors.[/bold red]"
                        )
                        logger.error(
                            f"Version stagnation: expected > {prev_major}, got {current_ver_str} (major {new_major})"
                        )
                        sys.exit(1)

                    console.print(f"[blue]Database is now at version: {current_ver_str}[/blue]")

        except KeyboardInterrupt:
            console.print("[bold red]Operation cancelled by user.[/bold red]")
            logger.info("Operation cancelled by user")
        except Exception as e:
            console.print(f"[bold red]Unexpected error:[/bold red] {e}")
            logger.exception("Unexpected error")
        finally:
            self.cleanup()