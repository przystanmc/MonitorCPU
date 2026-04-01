import tkinter as tk
from tkinter import ttk, messagebox
import psutil
import time
import wmi
import ctypes, sys
import threading
import pythoncom
import subprocess
import json
from collections import deque

# =====================================================================
#  UPRAWNIENIA ADMINA
# =====================================================================
def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except:
        return False

if not is_admin():
    try:
        ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, " ".join(sys.argv), None, 1)
        sys.exit()
    except Exception as e:
        messagebox.showerror("BŁĄD UPRAWNIEŃ", f"Nie można uruchomić jako Admin.\n{e}\nMonitor wymaga uprawnień administracyjnych.")
        sys.exit(1)


# =====================================================================
#  GŁÓWNA KLASA MONITORA
# =====================================================================
class TabbedMonitor:
    def __init__(self, root):
        self.root = root
        self.root.title("AI System Monitor v9 - Fixed Edition")
        self.root.geometry("500x850")
        self.root.attributes("-topmost", True)
        self.root.configure(bg="#1a1a1a")

        # ===== BLOKADY WĄTKÓW =====
        self._ai_lock      = threading.Lock()
        self._temp_lock    = threading.Lock()
        self._docker_lock  = threading.Lock()
        self._net_lock     = threading.Lock()

        # ===== CACHE PROCESÓW AI =====
        self._ai_lines: list[str] = []

        # ===== CACHE TEMP / TAKTOWANIE =====
        self._temp_cache  = 0.0
        self._freq_cache  = 0.0
        self._temp_method = "BRAK"
        self.cpu_temp_history = deque(maxlen=10)

        # ===== CACHE DOCKER =====
        self._docker_cache = "Ładowanie danych Docker..."
        self._docker_last_update = 0.0   # timestamp ostatniego odświeżenia

        # ===== CACHE SIECI (delta liczony w wątku) =====
        self._net_dl = 0.0
        self._net_up = 0.0
        self._last_net       = psutil.net_io_counters()
        self._last_net_time  = time.time()

        # ===== INNE ZMIENNE =====
        self.curr_pid             = psutil.Process().pid
        self.last_ram_used        = 0
        self.last_cpu_throttle_ts = 0.0
        self.ram_sticks_data: list = []

        # ===== CACHE WMI (statyczne) =====
        self.ram_hw_cache    = "Inicjalizacja sprzętu..."
        self.gpu_info_cache  = "Szukanie karty graficznej..."
        self.wmi_initialized = False
        self.wmi_error       = ""

        # ===== FLAGA AKTYWNEJ KARTY AI =====
        self.ai_scanning_active = False

        # ===== INICJALIZACJA WMI W TLE =====
        threading.Thread(target=self.init_all_wmi, daemon=True).start()

        # ===== STYL GUI =====
        self.style = ttk.Style()
        self.style.theme_use('clam')
        self.style.configure("TNotebook", background="#333", borderwidth=0)
        self.style.configure("TNotebook.Tab", background="#444", foreground="white",
                             padding=[25, 12], font=("Segoe UI", 11, "bold"))
        self.style.map("TNotebook.Tab",
                       background=[("selected", "#00ff00")],
                       foreground=[("selected", "black")])

        self.notebook = ttk.Notebook(self.root)
        self.tab_sys    = tk.Frame(self.notebook, bg="#1a1a1a")
        self.tab_ai     = tk.Frame(self.notebook, bg="#1a1a1a")
        self.tab_docker = tk.Frame(self.notebook, bg="#1a1a1a")
        self.notebook.add(self.tab_sys,    text="  💻 SYSTEM  ")
        self.notebook.add(self.tab_ai,     text="  🐍 PYTHON  ")
        self.notebook.add(self.tab_docker, text="  🐳 DOCKER  ")
        self.notebook.pack(expand=True, fill="both", padx=5, pady=5)
        self.notebook.bind("<<NotebookTabChanged>>", self.on_tab_change)

        self.setup_tabs()

        # ===== WĄTEK TŁA =====
        threading.Thread(target=self.background_worker, daemon=True).start()

        # ===== GŁÓWNA PĘTLA GUI =====
        self.update_gui_loop()

    # ------------------------------------------------------------------
    #  INICJALIZACJA WMI
    # ------------------------------------------------------------------
    def init_all_wmi(self):
        try:
            pythoncom.CoInitialize()
            print("--- INICJALIZACJA WMI ---")

            try:
                self.w_sys = wmi.WMI(namespace="root\\wmi")
                print("✓ root\\wmi OK")
            except Exception as e:
                self.w_sys = None
                self.wmi_error += f"WMI root\\wmi: {e}\n"
                print(f"✗ root\\wmi: {e}")

            try:
                self.w_cim = wmi.WMI()
                print("✓ root\\cimv2 OK")
            except Exception as e:
                self.w_cim = None
                self.wmi_error += f"WMI root\\cimv2: {e}\n"
                print(f"✗ root\\cimv2: {e}")

            try:
                self.w_ohm = wmi.WMI(namespace="root\\OpenHardwareMonitor")
                sensors = self.w_ohm.Sensor()
                print(f"✓ OHM OK – sensorów: {len(sensors)}")
                for s in sensors[:5]:
                    print(f"   {s.Name} ({s.SensorType}) = {s.Value}")
            except Exception as e:
                self.w_ohm = None
                self.wmi_error += f"OHM: {e}\n"
                print(f"✗ OHM: {e}")

            self._load_gpu_info()
            self._load_ram_info()
            self.wmi_initialized = True
            print("✓ WMI gotowy.")
        except Exception as e:
            self.wmi_initialized = False
            self.wmi_error = f"Krytyczny błąd WMI: {e}"
            print(f"✗ Błąd krytyczny WMI: {e}")
        finally:
            try:
                pythoncom.CoUninitialize()
            except:
                pass

    def _load_gpu_info(self):
        try:
            if not self.w_cim:
                self.gpu_info_cache = "GPU: WMI niedostępne\n"
                return
            gpus = self.w_cim.Win32_VideoController()
            gpu_rep = ""
            if not gpus:
                gpu_rep = "GPU: Brak wykrytych kart\n"
            else:
                for g in gpus:
                    gpu_rep += f"KARTA: {g.Name}\n"
                    try:
                        vram = abs(int(g.AdapterRAM)) // 1024**2 if g.AdapterRAM else 0
                        gpu_rep += f" └─ VRAM: {vram} MB\n"
                    except:
                        gpu_rep += f" └─ VRAM: Brak danych\n"
            self.gpu_info_cache = gpu_rep
        except Exception as e:
            self.gpu_info_cache = f"GPU: Błąd odczytu ({type(e).__name__})\n"
            print(f"Błąd GPU: {e}")

    def _load_ram_info(self):
        try:
            if not self.w_cim:
                self.ram_hw_cache = "RAM: WMI niedostępne\n"
                return
            vendors = {
                "802C": "Micron", "80AD": "SK Hynix", "80CE": "Samsung",
                "014F": "Transcend", "029E": "Corsair", "0198": "Kingston",
                "0089": "Intel", "0308": "Ramaxel", "0311": "A-DATA", "9801": "Kingston",
            }
            sticks = self.w_cim.Win32_PhysicalMemory()
            new_sticks = []
            if not sticks:
                self.ram_hw_cache = "RAM: Brak danych z BIOS\n"
                return
            hw = f"KONFIGURACJA: {len(sticks)} kości\n"
            for i, s in enumerate(sticks):
                try:
                    cap = int(s.Capacity) // 1024**3
                    raw_vendor = str(s.Manufacturer).strip() if s.Manufacturer else "Nieznany"
                    vendor_name = vendors.get(raw_vendor[:4], raw_vendor if raw_vendor != "Nieznany" else "Nieznany")
                    speed = str(s.Speed) if s.Speed else "N/A"
                    new_sticks.append({'slot': i, 'cap': cap, 'speed': speed})
                    hw += f" ├─ SLOT {i}: {cap}GB | {vendor_name} | {speed} MHz\n"
                except Exception as e:
                    hw += f" ├─ SLOT {i}: Błąd odczytu ({type(e).__name__})\n"
            self.ram_sticks_data = new_sticks
            self.ram_hw_cache = hw
        except Exception as e:
            self.ram_hw_cache = f"RAM: Błąd ({type(e).__name__})\n"
            print(f"Błąd RAM: {e}")

    # ------------------------------------------------------------------
    #  WĄTEK TŁA – temperatura, procesy AI, docker, sieć
    # ------------------------------------------------------------------
    def background_worker(self):
        """
        Jeden wątek tła obsługuje WSZYSTKIE ciężkie operacje:
          - odczyt temperatury CPU (WMI/ACPI) – co 2s
          - skanowanie procesów AI – co 1s (gdy karta aktywna)
          - Docker stats – co 5s
          - delta sieci – co 1s
        Każda sekcja zapisuje do cache'u z blokadą, GUI tylko odczytuje.
        """
        pythoncom.CoInitialize()

        # Lokalny licznik czasu dla rzadszych zadań
        last_temp_update   = 0.0
        last_docker_update = 0.0

        try:
            while True:
                now = time.time()

                # --- Temperatura CPU (co 2 sekundy) ---
                if now - last_temp_update >= 2.0:
                    t, f, m = self._read_temp_and_freq()
                    with self._temp_lock:
                        self._temp_cache  = t
                        self._freq_cache  = f
                        self._temp_method = m
                        if t > 0:
                            self.cpu_temp_history.append(t)
                    last_temp_update = now

                # --- Sieć (co 1 sekundę) ---
                self._update_net_delta()

                # --- Procesy AI (co 1 sekundę gdy karta aktywna) ---
                if self.ai_scanning_active:
                    self._scan_ai_processes()

                # --- Docker (co 5 sekund) ---
                if now - last_docker_update >= 5.0:
                    result = self._fetch_docker_stats()
                    with self._docker_lock:
                        self._docker_cache = result
                    last_docker_update = now

                time.sleep(1.0)
        finally:
            try:
                pythoncom.CoUninitialize()
            except:
                pass

    def _read_temp_and_freq(self) -> tuple[float, float, str]:
        """Odczyt temperatury i taktowania – TYLKO wywoływane z wątku tła."""
        temp, freq, method = 0.0, 0.0, "BRAK"

        # Próba 1: OpenHardwareMonitor
        try:
            pythoncom.CoInitialize()
            local_wmi = wmi.WMI(namespace="root\\OpenHardwareMonitor")
            sensors = local_wmi.Sensor()
            temp_values = []
            for s in sensors:
                try:
                    if s.SensorType == u'Temperature':
                        name = s.Name.upper()
                        if "CPU" in name or "CORE" in name:
                            temp_values.append(float(s.Value))
                except:
                    continue
            if temp_values:
                temp = max(temp_values)
                method = "OHM"
        except:
            pass
        finally:
            try:
                pythoncom.CoUninitialize()
            except:
                pass

        # Próba 2: ACPI fallback
        if temp == 0.0:
            try:
                pythoncom.CoInitialize()
                local_sys = wmi.WMI(namespace="root\\wmi")
                res = local_sys.MSAcpi_ThermalZoneTemperature()
                if res:
                    temp = (res[0].CurrentTemperature / 10.0) - 273.15
                    method = "ACPI"
                pythoncom.CoUninitialize()
            except:
                pass

        # Taktowanie przez psutil (lekkie, bez WMI)
        try:
            freq = psutil.cpu_freq().current / 1000.0
        except:
            freq = 0.0

        return temp, freq, method

    def _update_net_delta(self):
        """Oblicz prędkość sieci i zapisz do cache'u."""
        try:
            net  = psutil.net_io_counters()
            now  = time.time()
            with self._net_lock:
                dt = now - self._last_net_time
                if dt <= 0:
                    dt = 1.0
                self._net_dl = (net.bytes_recv - self._last_net.bytes_recv) / 1024 / dt
                self._net_up = (net.bytes_sent - self._last_net.bytes_sent) / 1024 / dt
                self._last_net      = net
                self._last_net_time = now
        except Exception as e:
            print(f"[WARN] Błąd sieci: {e}")

    def _scan_ai_processes(self):
        """Skanuj procesy Python/AI i zapisz do cache'u."""
        new_ai: list[str] = []
        try:
            for p in psutil.process_iter(['pid', 'name', 'cmdline', 'memory_info']):
                try:
                    pi = p.info
                    if pi['pid'] == self.curr_pid:
                        icon, label = "🖥️", "TEN MONITOR"
                    elif "python" in (pi['name'] or "").lower():
                        cmd = " ".join(pi['cmdline'] or []).lower()
                        if any(x in cmd for x in ["voice", "vosk", "wojtek"]):
                            icon, label = "🎙️", "WOJTEK-AI"
                        elif "discord" in cmd:
                            icon, label = "💬", "BOT DISCORD"
                        else:
                            icon, label = "🐍", "Skrypt Python"
                    else:
                        continue

                    cpu = p.cpu_percent(interval=0)
                    mem = pi['memory_info'].rss / (1024**2)
                    new_ai.append(f"● {icon} {label}\n   └── CPU: {cpu:>5.1f}% | RAM: {mem:>4.0f} MB")
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    continue
                except Exception as e:
                    print(f"[WARN] Błąd skanowania procesu: {e}")
                    continue

            with self._ai_lock:
                self._ai_lines = new_ai
        except Exception as e:
            print(f"[ERROR] _scan_ai_processes: {e}")

    def _fetch_docker_stats(self) -> str:
        """Pobierz statystyki Docker – wywoływane z wątku tła."""
        try:
            # CREATE_NO_WINDOW – ukrywa okno konsoli na Windows
            CREATE_NO_WINDOW = 0x08000000
            result = subprocess.run(
                ["docker", "stats", "--no-stream", "--format", "{{json .}}"],
                capture_output=True, text=True, timeout=4,
                creationflags=CREATE_NO_WINDOW
            )
            if result.returncode != 0:
                return "Docker daemon nie działa lub brak uprawnień."

            lines = result.stdout.strip().splitlines()
            if not lines:
                return "Brak aktywnych kontenerów."

            containers = []
            total_cpu = 0.0
            for line in lines:
                data = json.loads(line)
                name = data.get("Name", "unknown")
                cpu  = float(data.get("CPUPerc", "0").replace("%", ""))
                mem  = data.get("MemPerc", "?")
                total_cpu += cpu
                containers.append(
                    f"🐳 {name}\n"
                    f"   └─ CPU: {cpu:.2f}% | RAM: {mem}"
                )

            header = (
                f"AKTYWNE KONTENERY: {len(containers)}\n"
                f"SUMARYCZNE CPU:    {total_cpu:.2f}%\n\n"
            )
            return header + "\n\n".join(containers)

        except FileNotFoundError:
            return "Docker nie jest zainstalowany."
        except subprocess.TimeoutExpired:
            return "Docker: timeout (daemon zajęty)."
        except Exception as e:
            return f"Błąd Dockera: {e}"

    # ------------------------------------------------------------------
    #  DETEKCJA ANOMALII
    # ------------------------------------------------------------------
    def _detect_thermal_throttle(self, temp: float, cpu_total: float) -> str:
        if temp > 95 and cpu_total < 30:
            now = time.time()
            if now - self.last_cpu_throttle_ts > 30:
                self.last_cpu_throttle_ts = now
                return "⚠️  THERMAL THROTTLE: Temp > 95°C przy niskim obciążeniu CPU"
        return ""

    def _detect_ram_spike(self) -> str:
        try:
            ram   = psutil.virtual_memory()
            delta = ram.used - self.last_ram_used
            self.last_ram_used = ram.used
            if delta > ram.total * 0.1:
                return f"⚠️  RAM SPIKE: +{delta // 1024**2} MB"
        except Exception as e:
            print(f"[WARN] RAM spike check: {e}")
        return ""

    # ------------------------------------------------------------------
    #  FORMAT RDZENI CPU
    # ------------------------------------------------------------------
    def _get_cpu_core_display(self, cpu_cores: list[float]) -> str:
        if len(cpu_cores) > 8:
            report = f"CPU CORES ({len(cpu_cores)} rdz):\n"
            for i in range(0, len(cpu_cores), 4):
                chunk = cpu_cores[i:i+4]
                report += "  "
                for j, c in enumerate(chunk):
                    bar = "░░░" if c < 30 else ("▒▒▒" if c < 60 else "███")
                    report += f"R{i+j}: {c:>3.0f}% {bar}  "
                report += "\n"
        else:
            report = "CPU CORES:\n"
            for i, c in enumerate(cpu_cores):
                report += f"  ├─ R{i}: {c:>5.1f}%\n"
        return report

    # ------------------------------------------------------------------
    #  SETUP GUI
    # ------------------------------------------------------------------
    def setup_tabs(self):
        colors = {"sys": "#00ff00", "ai": "#f0f", "docker": "#00d0ff"}
        for tab, attr, color in [
            (self.tab_sys,    "txt_sys",    colors["sys"]),
            (self.tab_ai,     "txt_ai",     colors["ai"]),
            (self.tab_docker, "txt_docker", colors["docker"]),
        ]:
            tk.Label(tab, text="MONITOR SYSTEMOWY", fg=color, bg="#1a1a1a",
                     font=("Consolas", 11, "bold")).pack(pady=5)
            txt = tk.Text(tab, height=42, width=65, bg="#000", fg=color,
                          font=("Consolas", 9), bd=0, padx=10, pady=10)
            txt.pack(pady=5, padx=10)
            setattr(self, attr, txt)

            btn = tk.Button(tab, text="📋 KOPIUJ DANE",
                            bg=color, font=("Arial", 9, "bold"))
            btn.configure(command=lambda t=txt, b=btn, c=color: self.copy_data(t, b, c))
            btn.pack(pady=5)

    def on_tab_change(self, event):
        current_tab = self.notebook.index(self.notebook.select())
        self.ai_scanning_active = (current_tab == 1)
        state = "AKTYWNE" if self.ai_scanning_active else "UŚPIONE"
        print(f"[TAB] Skanowanie AI: {state}")

    # ------------------------------------------------------------------
    #  GŁÓWNA PĘTLA GUI (tylko odczyt z cache'u – bez blokujących operacji)
    # ------------------------------------------------------------------
    def update_gui_loop(self):
        try:
            # ===== CPU STATS =====
            # Jedno wywołanie percpu=True, średnia ręcznie – spójne dane
            cpu_cores = psutil.cpu_percent(percpu=True)
            cpu_total = sum(cpu_cores) / len(cpu_cores) if cpu_cores else 0.0

            uptime = int(time.time() - psutil.boot_time())
            h, m   = divmod(uptime // 60, 60)

            # Odczyt z cache'u (thread-safe)
            with self._temp_lock:
                temp   = self._temp_cache
                freq   = self._freq_cache
                method = self._temp_method

            report = "--- SYSTEM & CPU ---\n"
            report += f"UPTIME:  {h}h {m}m | PROCESY: {len(psutil.pids())}\n"

            if temp > 0:
                report += f"OGÓLNE:  {cpu_total:.1f}% | {freq:.2f} GHz | {temp:.1f}°C ({method})\n"
            else:
                report += f"OGÓLNE:  {cpu_total:.1f}% | {freq:.2f} GHz | --°C (Brak danych)\n"

            report += self._get_cpu_core_display(cpu_cores)

            # ===== RAM =====
            ram = psutil.virtual_memory()
            bar = '#' * int(ram.percent // 5) + '.' * (20 - int(ram.percent // 5))
            report += f"\n--- PAMIĘĆ (DYNAMICZNIE) ---\n"
            report += f"ZUŻYCIE:   [{bar}] {ram.percent}%\n"
            report += f"W UŻYCIU:  {ram.used  // 1024**2} MB / {ram.total // 1024**2} MB\n"
            report += f"DOSTĘPNE:  {ram.available // 1024**2} MB\n"

            ram_spike = self._detect_ram_spike()
            if ram_spike:
                report += f"{ram_spike}\n"

            if self.ram_sticks_data:
                report += "SZCZEGÓŁY MODUŁÓW:\n"
                for stick in self.ram_sticks_data:
                    stick_usage = (ram.used / ram.total) * stick['cap']
                    report += f" ├─ SLOT {stick['slot']}: {stick_usage:.1f}/{stick['cap']} GB | {stick['speed']} MHz\n"

            # ===== GPU =====
            report += f"\n--- GPU & VRAM ---\n{self.gpu_info_cache}"

            # ===== SIEĆ (z cache'u wątku tła) =====
            with self._net_lock:
                dl = self._net_dl
                up = self._net_up
            report += f"\n--- SIEĆ ---\n DL: {dl:>7.1f} KB/s | UP: {up:>7.1f} KB/s\n"

            # ===== DYSKI =====
            report += "\n--- DYSKI ---\n"
            for p in psutil.disk_partitions():
                if 'cdrom' in p.opts or not p.fstype:
                    continue
                try:
                    u = psutil.disk_usage(p.mountpoint)
                    report += f" {p.device:<4} {u.percent:>3}% [{u.used // 1024**3}G/{u.total // 1024**3}G]\n"
                except (OSError, PermissionError):
                    continue

            # ===== THROTTLE =====
            throttle_alert = self._detect_thermal_throttle(temp, cpu_total)
            if throttle_alert:
                report += f"\n{throttle_alert}\n"

            # ===== SPRZĘT BIOS =====
            report += f"\n--- SPRZĘT (BIOS) ---\n{self.ram_hw_cache}"

            # ===== BŁĘDY WMI =====
            if self.wmi_error and not self.wmi_initialized:
                report += f"\n--- BŁĘDY WMI ---\n{self.wmi_error}"

            # ===== UPDATE WIDGETÓW =====
            if not self.txt_sys.tag_ranges("sel"):
                self.txt_sys.delete("1.0", tk.END)
                self.txt_sys.insert("1.0", report + f"\nAktualizacja: {time.strftime('%H:%M:%S')}")

            # ===== TAB AI (z cache'u wątku tła) =====
            if not self.txt_ai.tag_ranges("sel"):
                self.txt_ai.delete("1.0", tk.END)
                ai_text = "--- AKTYWNE PROCESY AI ---\n\n"
                with self._ai_lock:
                    lines_snapshot = list(self._ai_lines)
                if lines_snapshot:
                    ai_text += "\n\n".join(lines_snapshot)
                else:
                    if self.ai_scanning_active:
                        ai_text += "Skanowanie..."
                    else:
                        ai_text += "Otwórz zakładkę 'PYTHON' aby skanować procesy."
                self.txt_ai.insert("1.0", ai_text)

            # ===== TAB DOCKER (z cache'u wątku tła) =====
            if not self.txt_docker.tag_ranges("sel"):
                with self._docker_lock:
                    docker_text = self._docker_cache
                self.txt_docker.delete("1.0", tk.END)
                self.txt_docker.insert("1.0", "--- DOCKER CONTAINERS ---\n\n" + docker_text)

        except Exception as e:
            import traceback
            print(f"[ERROR] Błąd GUI: {e}")
            traceback.print_exc()

        self.root.after(1000, self.update_gui_loop)

    # ------------------------------------------------------------------
    #  KOPIOWANIE Z WIZUALNYM FEEDBACKIEM (bez irytującego messagebox)
    # ------------------------------------------------------------------
    def copy_data(self, widget: tk.Text, btn: tk.Button, color: str):
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(widget.get("1.0", tk.END).strip())
            original_text = btn.cget("text")
            btn.configure(text="✅ SKOPIOWANO!", bg="#006600")
            self.root.after(1500, lambda: btn.configure(text=original_text, bg=color))
        except Exception as e:
            print(f"[WARN] Błąd kopiowania: {e}")


# =====================================================================
#  MAIN
# =====================================================================
if __name__ == "__main__":
    root = tk.Tk()
    app  = TabbedMonitor(root)
    root.mainloop()