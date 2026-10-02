from multiprocessing import freeze_support

if __name__ == "__main__":
    freeze_support()
    from scisaurus.desktop.backend import main
    raise SystemExit(main())
