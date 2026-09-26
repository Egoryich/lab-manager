# Физический сервер Proxmox — характеристики и схема дисков

Дата фиксации: 20.09.2026

## 1. Физический сервер

### Материнская плата

- Производитель: LJIT
- Модель: X99-D3 V8.23
- Ревизия: V8.23
- BIOS: American Megatrends Inc.
- Версия BIOS: 5.11
- Дата BIOS: 19.03.2025

### Процессор

- Модель: Intel Xeon E5-2673 v3
- Сокетов: 1
- Физических ядер: 12
- Потоков: 24
- Потоков на ядро: 2
- Базовая частота: 2.40 ГГц
- Максимальная частота по системе: до 3.10 ГГц
- L3 cache: 30 MiB
- Аппаратная виртуализация Intel VT-x: поддерживается и включена
- KVM: `kvm_intel` и `kvm` загружены

### Оперативная память

- Установлено: 32 ГБ
- Linux видит: около 31 GiB
- `MemTotal`: 32 760 852 kB
- Swap: 4 ГБ
- Тип памяти: DDR3 ECC
- Частота: 1866 MT/s
- Обнаруженные модули: Samsung M393B1G70QH0-CMA
- ECC: Multi-bit ECC

Примечание: DMI показывает не все установленные DIMM корректно, однако операционная система фактически видит полный объём около 32 ГБ.

### Сетевая карта

- Контроллер: Realtek RTL8111/8168/8211/8411
- Интерфейс Proxmox: `nic0`
- Тип: PCI Express Gigabit Ethernet
- Скорость класса: 1 Гбит/с

---

## 2. Proxmox

- Proxmox VE: 9.2.0
- `pve-manager`: 9.2.20
- Debian: 13 (trixie)
- Kernel: `7.0.14-17-pve`
- QEMU/KVM: `pve-qemu-kvm 11.0.3-3`
- LXC: `lxc-pve 7.0.0-2`

Management IP:

```text
192.168.0.123/24
```

Gateway:

```text
192.168.0.1
```

Web UI:

```text
https://192.168.0.123:8006
```

---

## 3. Физические диски

### `/dev/sda` — основной student storage

- Модель: WDC WD5000AAKX-00ERMA0
- Тип: HDD
- Физический объём: 500 ГБ
- Отображаемый объём: около 465.8 GiB
- SMART: PASSED
- Назначение: студенческие LXC и VM
- Proxmox storage: `student-lvm`
- Тип storage: LVM-Thin
- Доступный thin pool: около 456.3 GiB

На диске уже расположен тестовый контейнер:

```text
CT 201
Disk: 10 GB
```

### `/dev/sdb` — системный SSD

- Модель: WDC WDS120G2G0A-00JH30
- Тип: SATA SSD
- Физический объём: 120 ГБ
- Отображаемый объём: около 111.8 GiB
- SMART: PASSED
- Назначение: Proxmox VE и служебное хранилище

Разметка:

```text
/dev/sdb
├── /dev/sdb1      ~1 MB     служебный раздел
├── /dev/sdb2       1 GB     EFI /boot/efi
└── /dev/sdb3     110 GB     LVM
    ├── pve-swap     4 GB    swap
    ├── pve-root    32 GB    /
    └── pve-data    ~63 GB   local-lvm
```

### Выведенный из эксплуатации диск

Ранее в сервере использовался:

- Hitachi HDP725040GLA360
- 400 ГБ
- 7200 rpm
- наработка около 49 334 часов
- были зафиксированы reallocated sectors и CRC errors

Этот диск решено не использовать дальше.

---

## 4. Storage в Proxmox

Текущие storage:

```text
local
Type: Directory
Назначение:
- ISO
- LXC templates
- backup
- служебные файлы
```

```text
local-lvm
Type: LVM-Thin
Расположение: системный SSD /dev/sdb
Объём: около 63 GB
Назначение:
- Disk image
- Container
```

```text
student-lvm
Type: LVM-Thin
Расположение: HDD /dev/sda
Объём: около 456 GB
Назначение:
- Student LXC
- Student VM
- Demo VM
```

На момент проверки:

```text
local        ~31 GB total   ~19% used
local-lvm    ~63 GB total   ~1.5% used
student-lvm ~456 GB total   ~0.2% used
```

---

## 5. Схема дисков

```text
Физический сервер
│
├── SSD 120 GB — WDC WDS120G2G0A
│   ├── EFI
│   ├── swap 4 GB
│   ├── root 32 GB
│   │   └── Proxmox VE
│   └── local-lvm ~63 GB
│       └── служебные VM/LXC
│
└── HDD 500 GB — WDC WD5000AAKX
    └── student-lvm ~456 GB
        ├── Student LXC
        ├── Student VM
        ├── Demo VM
        └── CT 201 — test-student-01
```

---

## 6. Краткий итог

```text
CPU:
Intel Xeon E5-2673 v3
12 ядер / 24 потока
2.40–3.10 ГГц
30 MiB L3
VT-x

RAM:
32 ГБ DDR3 ECC
1866 MT/s

Network:
Realtek 1 Gbit/s

System disk:
SSD 120 ГБ

Student storage:
HDD 500 ГБ
LVM-Thin ~456 ГБ

Proxmox:
VE 9.2.0
Debian 13
Kernel 7.0.14-17-pve
```
