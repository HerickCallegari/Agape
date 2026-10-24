#include <limits.h>
#include <mach-o/dyld.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#if defined(__arm64__)
#define AGAPE_NATIVE_ARCH "arm64"
#elif defined(__x86_64__)
#define AGAPE_NATIVE_ARCH "x86_64"
#else
#error Arquitetura macOS não suportada
#endif

int main(int argc, char *argv[]) {
    (void)argc;

    char executable_path[PATH_MAX];
    uint32_t executable_path_size = sizeof(executable_path);
    if (_NSGetExecutablePath(executable_path, &executable_path_size) != 0) {
        fprintf(stderr, "Não foi possível determinar o caminho do launcher.\n");
        return 1;
    }

    char resolved_path[PATH_MAX];
    if (realpath(executable_path, resolved_path) == NULL) {
        perror("Não foi possível resolver o caminho do launcher");
        return 1;
    }

    char *macos_separator = strrchr(resolved_path, '/');
    if (macos_separator == NULL) {
        fprintf(stderr, "Estrutura do bundle macOS inválida.\n");
        return 1;
    }
    *macos_separator = '\0';

    char *contents_separator = strrchr(resolved_path, '/');
    if (contents_separator == NULL) {
        fprintf(stderr, "Estrutura do bundle macOS inválida.\n");
        return 1;
    }
    *contents_separator = '\0';

    char native_executable[PATH_MAX];
    int written = snprintf(
        native_executable,
        sizeof(native_executable),
        "%s/Resources/native/%s/ClinicaAgape.app/Contents/MacOS/ClinicaAgape",
        resolved_path,
        AGAPE_NATIVE_ARCH
    );
    if (written < 0 || (size_t)written >= sizeof(native_executable)) {
        fprintf(stderr, "O caminho do aplicativo nativo excede o limite permitido.\n");
        return 1;
    }

    argv[0] = native_executable;
    execv(native_executable, argv);
    perror("Não foi possível iniciar o aplicativo nativo");
    return 1;
}
