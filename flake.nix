{
  "description" = "Development shell for the OCIR image update Oracle Function";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs { inherit system; };
    in {
      devShells.${system}.default = pkgs.mkShell {
        packages = [
          pkgs.go_1_25
        ];

        CGO_ENABLED = "0";
      };
    };
}
